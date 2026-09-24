"""
Folio — Web backend (FastAPI).

Run with:
    uvicorn web.server:app --reload
or:
    python -m web.server
"""

import logging
import os
import random
import re
import shutil
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import (
    HASH_INDEX_FILE,
    RECENT_FILE,
    SCAN_CACHE_FILE,
    SETLISTS_FILE,
    AnnotationConflictError,
    SafeJSON,
    SafeJSONError,
    Score,
    annotation_sidecar_path,
    is_library_relative,
    load_annotations,
    load_hash_index,
    load_recent,
    load_scan_cache,
    load_setlists,
    map_recent_paths,
    map_song_paths,
    migrate_to_relative,
    normalize_path,
    portable_path,
    remap_hash_index,
    remap_recent_paths,
    remap_setlist_paths,
    rename_score_tags,
    save_annotations,
    save_hash_index,
    save_recent,
    save_scan_cache,
    save_setlists,
    scan_library,
    to_stored_path,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_FORMAT = "%(levelprefix)s %(asctime)s %(message)s"
ACCESS_FORMAT = '%(levelprefix)s %(asctime)s %(client_addr)s - "%(request_line)s" %(status_code)s'

log = logging.getLogger("folio")

# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".folio")

# Migrate from old config directory
_OLD_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".music_score_viewer")
if os.path.isdir(_OLD_CONFIG_DIR) and not os.path.exists(CONFIG_DIR):
    os.rename(_OLD_CONFIG_DIR, CONFIG_DIR)
os.makedirs(CONFIG_DIR, exist_ok=True)
WEB_CONFIG_PATH = os.path.join(CONFIG_DIR, "web_config.json")

DEFAULT_KEYBINDINGS = {
    # Navigation views (work everywhere)
    "go_library": "Alt+l",
    "go_setlists": "Alt+s",
    "go_recent": "Alt+r",
    "go_newest": "Alt+n",
    "focus_search": "Ctrl+f",
    "reset_filters": "Ctrl+r",
    # Viewer tools
    "tool_nav": "v",
    "tool_pen": "d",
    "tool_text": "t",
    "tool_eraser": "e",
    "tool_move": "m",
    "toggle_fullscreen": "f",
    "add_to_setlist": "s",
    "edit_tags": "g",
    "rotate_cw": "r",
    "rotate_ccw": "Shift+r",
    "undo": "Ctrl+z",
    "close_score": "Escape",
    # Page navigation
    "next_page": "ArrowRight",
    "prev_page": "ArrowLeft",
    "first_page": "Home",
    "last_page": "End",
}

DEFAULT_WEB_CONFIG: dict = {
    "last_directory": "",
    "allowed_roots": [],
    "keybindings": {},
}

# Max setlist name length; only printable non-path characters allowed
_MAX_SETLIST_NAME = 200
_SETLIST_NAME_RE = re.compile(r'^[^/\\<>:"|?*\x00-\x1f]+$')


# Keys written by the removed authentication mechanism; dropped on load.
_OBSOLETE_CONFIG_KEYS = ("auth_salt", "session_secret")


def _load_config() -> dict:
    try:
        data = SafeJSON.load(WEB_CONFIG_PATH, default={})
    except SafeJSONError:
        data = {}
    stale = [k for k in _OBSOLETE_CONFIG_KEYS if k in data]
    if stale:
        for key in stale:
            del data[key]
        log.info("Removed obsolete config keys: %s", ", ".join(stale))
        _save_config(data)
    merged = {**DEFAULT_WEB_CONFIG, **data}
    return merged


def _save_config(cfg: dict) -> None:
    SafeJSON.save(WEB_CONFIG_PATH, cfg)


class AppState:
    """Mutable server-wide state.

    ``scores`` is indexed by path: assign a new list or use replace_score(),
    never mutate the list in place.
    """

    _scores: list[Score]
    _index: dict[str, int]  # normalize_path(filepath) -> position in _scores

    def __init__(self) -> None:
        self.config: dict = _load_config()
        self.library_dir: str = ""
        self.scores = []

    @property
    def scores(self) -> list[Score]:
        return self._scores

    @scores.setter
    def scores(self, scores: list[Score]) -> None:
        self._scores = scores
        self._index = {}
        for i, s in enumerate(scores):
            self._index.setdefault(normalize_path(s.filepath), i)

    def find_score(self, path: str) -> Score | None:
        """The library score at *path* (any accepted form), or None."""
        i = self._index.get(normalize_path(path))
        return None if i is None else self._scores[i]

    def replace_score(self, old: Score, new: Score) -> None:
        """Put *new* in *old*'s place (e.g. after a rename)."""
        i = self._index.pop(normalize_path(old.filepath))
        self._scores[i] = new
        self._index[normalize_path(new.filepath)] = i

    def set_library(self, path: str) -> None:
        path = normalize_path(path)
        self.library_dir = path
        _migrate_to_relative(path)
        cache_path = self.scan_cache_path()
        try:
            hash_cache = load_scan_cache(cache_path, path)
        except SafeJSONError as e:
            log.warning("Could not load scan cache: %s", e)
            hash_cache = {}
        self.scores = scan_library(path, hash_cache=hash_cache)
        if self.scores:
            try:
                save_scan_cache(cache_path, hash_cache, path)
            except SafeJSONError as e:
                log.warning("Could not save scan cache: %s", e)
        _heal_references(self)
        self.config["last_directory"] = portable_path(path)
        _save_config(self.config)
        log.info(
            f"Library set to {path} — {len(self.scores)} scores found"
        )

    def setlist_path(self) -> str:
        if self.library_dir:
            return os.path.join(self.library_dir, SETLISTS_FILE)
        return os.path.join(CONFIG_DIR, SETLISTS_FILE)

    def hash_index_path(self) -> str:
        return os.path.join(self.library_dir, HASH_INDEX_FILE)

    def scan_cache_path(self) -> str:
        return os.path.join(self.library_dir, SCAN_CACHE_FILE)

    def recent_path(self) -> str:
        if self.library_dir:
            return os.path.join(self.library_dir, RECENT_FILE)
        return os.path.join(CONFIG_DIR, RECENT_FILE)


def _migrate_to_relative(library_dir: str) -> None:
    """Convert the library's stored paths to relative form, logging what
    changed and any paths that point outside the library."""
    try:
        report = migrate_to_relative(library_dir)
    except SafeJSONError as e:
        log.warning("Could not migrate stored paths to relative form: %s", e)
        return
    for name, (converted, foreign) in report.items():
        if converted:
            log.info("Converted %d path(s) in %s to library-relative form",
                     converted, name)
        if foreign:
            log.warning("%s: %d path(s) outside the library left as-is: %s",
                        name, len(foreign), ", ".join(foreign[:5]))


def _heal_references(st: "AppState") -> None:
    """Compare content hashes against the previous index to detect renames.

    Heals setlist paths and annotation sidecars, then saves the new index.

    Hashes shared by more than one live file are omitted from the index:
    duplicate content cannot distinguish a rename from a copy.

    The existing index is left untouched, rather than rebuilt, when the scan
    produced no usable hashes or when a heal write failed -- in the latter
    case so the next scan re-derives the same remap and retries.
    """
    index_path = st.hash_index_path()

    # Build hash -> paths.  A hash claimed by more than one live file carries
    # no rename signal, so it is kept out of both the index and the remap.
    by_hash: dict[str, list[str]] = defaultdict(list)
    for s in st.scores:
        if s.content_hash:
            by_hash[s.content_hash].append(portable_path(s.filepath))

    new_index: dict[str, str] = {}
    for h, paths in by_hash.items():
        if len(paths) > 1:
            log.warning("Duplicate content in %d files, skipping rename "
                        "detection: %s", len(paths), ", ".join(sorted(paths)))
            continue
        new_index[h] = paths[0]

    # A scan that yielded no usable hashes carries no rename information, and
    # rebuilding from it would wipe every known hash -- an unmounted library
    # volume, or a share where every read failed.  Leave the index as it is.
    if not new_index:
        if os.path.exists(index_path):
            log.warning("Scan produced no content hashes, "
                        "keeping existing hash index")
        return

    # Every path in the library, ambiguous hashes included: distinguishes
    # "renamed away" from "still on disk".
    live_paths = {portable_path(s.filepath) for s in st.scores}

    # Load previous index
    try:
        old_index = load_hash_index(index_path, st.library_dir)
    except SafeJSONError:
        old_index = {}

    # Detect renames: same hash, different path
    remap: dict[str, str] = {}
    for h, old_path in old_index.items():
        if h in new_index and new_index[h] != old_path:
            # Only remap if the old path no longer exists in the library
            if old_path not in live_paths:
                remap[old_path] = new_index[h]

    healed_ok = True
    if remap:
        log.info("Detected %d renamed score(s), healing references", len(remap))
        _heal_annotation_sidecars(remap)
        try:
            _heal_setlist_paths(remap)
        except SafeJSONError as e:
            healed_ok = False
            log.warning("Could not heal setlist paths: %s", e)
        try:
            _heal_recent_paths(remap)
        except SafeJSONError as e:
            healed_ok = False
            log.warning("Could not heal recent paths: %s", e)

    if not healed_ok:
        # Keep the old index so the next scan re-derives this remap and tries
        # again; the heal helpers are idempotent.
        log.warning("Heal incomplete, keeping previous hash index to retry")
        return

    # Save new index
    try:
        save_hash_index(index_path, new_index, st.library_dir)
    except SafeJSONError as e:
        log.warning("Could not save hash index: %s", e)


def _heal_annotation_sidecars(remap: dict[str, str]) -> None:
    """Rename annotation sidecar files to follow their PDFs."""
    for old_path, new_path in remap.items():
        old_sidecar = annotation_sidecar_path(old_path)
        if not os.path.exists(old_sidecar):
            continue
        new_sidecar = annotation_sidecar_path(new_path)
        if os.path.exists(new_sidecar):
            log.warning("Sidecar conflict: both %s and %s exist, skipping",
                        old_sidecar, new_sidecar)
            continue
        try:
            os.makedirs(os.path.dirname(new_sidecar), exist_ok=True)
            shutil.move(old_sidecar, new_sidecar)
            log.info("Moved annotation sidecar: %s -> %s",
                     old_sidecar, new_sidecar)
        except OSError as e:
            log.warning("Failed to move sidecar %s: %s", old_sidecar, e)


def _heal_setlist_paths(remap: dict[str, str]) -> None:
    """Rewrite setlist song paths through *remap* (old portable -> new)."""
    data = _load_setlists()
    if remap_setlist_paths(data, remap):
        _save_setlists(data)


def _heal_recent_paths(remap: dict[str, str]) -> None:
    """Rewrite recent-list filepaths through *remap* (old portable -> new)."""
    data = _load_recent()
    if remap_recent_paths(data, remap):
        _save_recent(data)


state = AppState()


def _auto_load_library() -> None:
    """Open the last-used library, if it still exists."""
    last = state.config.get("last_directory", "")
    if not last:
        return
    resolved = normalize_path(last)
    if os.path.isdir(resolved):
        try:
            state.set_library(resolved)
        except Exception as e:
            log.warning(f"Could not auto-load library {resolved}: {e}")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Update format on uvicorn's existing formatters (preserves color support)
    for name, fmt in [("uvicorn", LOG_FORMAT), ("uvicorn.access", ACCESS_FORMAT)]:
        for handler in logging.getLogger(name).handlers:
            f = handler.formatter
            f._fmt = fmt
            f._style._fmt = fmt
            f.datefmt = LOG_DATEFMT
    # Route app logging through uvicorn's logger so all output is colored
    uv = logging.getLogger("uvicorn")
    log.handlers = uv.handlers
    log.setLevel(uv.level)
    log.propagate = False
    log.info("Folio v%s starting", app.version)
    # Scan here rather than at import, so importing the module (e.g. in
    # tests) doesn't scan the configured library.
    _auto_load_library()
    yield


app = FastAPI(
    title="Folio", version="2.14.1",
    docs_url=None, redoc_url=None, lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Security: rate limiting
# ---------------------------------------------------------------------------

_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 5.0  # seconds
_RATE_LIMIT = 30  # max requests per window per key


def _check_rate_limit(key: str) -> None:
    now = time.monotonic()
    bucket = _rate_buckets[key]
    _rate_buckets[key] = bucket = [t for t in bucket if now - t < _RATE_WINDOW]
    if len(bucket) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests")
    bucket.append(now)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    client = request.client.host if request.client else "unknown"

    # Rate-limit write endpoints to prevent abuse
    if request.method in ("POST", "PUT", "DELETE"):
        _check_rate_limit(f"{client}:{path}")

    response = await call_next(request)

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "worker-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' https://cdn.jsdelivr.net; "
        "img-src 'self' blob: data:; "
        "font-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _resolve_under_library(filepath: str) -> str:
    """Resolve *filepath* (relative to the library root, as the API returns
    it) and verify it is under the library root.

    Returns the normalised absolute path.  Raises 400 if no library is set,
    403 on traversal attempts.  Does NOT check whether the file exists.
    """
    if not state.library_dir:
        raise HTTPException(status_code=400, detail="No library directory set")
    p = portable_path(filepath)
    if is_library_relative(p):
        p = os.path.join(state.library_dir, p)
    resolved = os.path.realpath(normalize_path(p))
    root = os.path.realpath(state.library_dir)
    if not resolved.startswith(root + os.sep) and resolved != root:
        raise HTTPException(status_code=403, detail="Path outside library")
    return resolved


def _validate_library_path(filepath: str) -> str:
    """Resolve *filepath*, verify it is under the library root **and exists**.

    Returns the normalised absolute path.
    """
    resolved = _resolve_under_library(filepath)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


def _api_path(path: str) -> str:
    """The form the API gives *path* in: relative to the library root, so
    clients (and their offline caches) don't depend on where it's mounted."""
    return to_stored_path(path, state.library_dir)


def _api_songs(items: list[dict]) -> list[dict]:
    """Setlist *items* with song paths in API form (modified in place)."""
    map_song_paths(items, _api_path)
    return items


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


class SetLibraryRequest(BaseModel):
    path: str


def _validate_setlist_name(name: str) -> str:
    """Validate and return a cleaned setlist name, or raise 400."""
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    if len(name) > _MAX_SETLIST_NAME:
        raise HTTPException(status_code=400, detail="Name too long")
    if not _SETLIST_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Name contains invalid characters")
    return name


def _is_allowed_root(path: str) -> bool:
    """Check whether *path* is under one of the configured allowed_roots.

    If no roots are configured, any directory is allowed (first-use convenience).
    Once roots are set, the library directory must be under one of them.
    """
    roots = state.config.get("allowed_roots", [])
    if not roots:
        return True
    resolved = os.path.realpath(path)
    for root in roots:
        root_resolved = os.path.realpath(normalize_path(root))
        if resolved == root_resolved or resolved.startswith(root_resolved + os.sep):
            return True
    return False


class ClientLogRequest(BaseModel):
    level: str = "error"
    message: str = ""
    detail: str = ""
    url: str = ""
    ua: str = ""


@app.post("/api/clientlog")
def client_log(req: ClientLogRequest):
    """Receive a client-side log line so iPad errors can be read from the
    server's stdout — Safari Web Inspector requires a Mac, which the user
    doesn't have. Opt-in: client only POSTs when localStorage.folioRemoteLog=1.
    Payload fields are truncated to bound abuse."""
    msg = (req.message or "")[:500]
    detail = (req.detail or "")[:2000]
    url = (req.url or "")[:500]
    ua = (req.ua or "")[:200]
    level = req.level if req.level in ("error", "warn", "info") else "error"
    log_fn = {"error": log.error, "warn": log.warning, "info": log.info}[level]
    log_fn("[client] %s | %s | url=%s | ua=%s", msg, detail, url, ua)
    return {"ok": True}


@app.get("/api/config")
def get_config():
    user_keys = state.config.get("keybindings", {})
    keybindings = {**DEFAULT_KEYBINDINGS, **user_keys}
    return {
        "library_dir": portable_path(state.library_dir),
        "score_count": len(state.scores),
        "version": app.version,
        "keybindings": keybindings,
    }


@app.post("/api/library")
def set_library(req: SetLibraryRequest):
    path = normalize_path(req.path)
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="Directory not found")
    if not _is_allowed_root(path):
        raise HTTPException(status_code=403, detail="Directory not in allowed roots")
    state.set_library(path)
    return {
        "library_dir": portable_path(state.library_dir),
        "score_count": len(state.scores),
    }


@app.post("/api/library/rescan")
def rescan_library():
    """Re-scan the current library directory to pick up added/removed files."""
    if not state.library_dir:
        raise HTTPException(status_code=400, detail="No library directory set")
    state.set_library(state.library_dir)
    return {
        "library_dir": portable_path(state.library_dir),
        "score_count": len(state.scores),
    }


class UpdateTagsRequest(BaseModel):
    path: str
    filename_tags: list[str]


def _find_score(resolved: str) -> Score:
    """Return the library score at *resolved*, or 404."""
    score = state.find_score(resolved)
    if score is None:
        raise HTTPException(status_code=404, detail="Score not found in library")
    return score


@app.get("/api/scores")
def get_score(path: str = Query(..., description="Score filepath")):
    """Return one score, including its folder/filename tag split.

    The library list is filtered by the client's current search, so a caller
    holding only a filepath cannot rely on it to recover a score's tags.
    """
    return _find_score(_validate_library_path(path)).to_dict(state.library_dir)


@app.put("/api/scores/tags")
def update_score_tags(req: UpdateTagsRequest):
    """Update the filename tags on a score, renaming the file on disk."""
    score = _find_score(_validate_library_path(req.path))

    # Clean tags: lowercase, alphanumeric + hyphens only
    clean_tags = set()
    for t in req.filename_tags:
        t = re.sub(r'[^\w-]', '', t.strip().lower())
        if t:
            clean_tags.add(t)

    try:
        new_score = rename_score_tags(score, clean_tags)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Rename failed: {e}")

    # Update in-memory library
    state.replace_score(score, new_score)

    # Update setlist references that point to the old path
    old_portable = portable_path(score.filepath)
    new_portable = portable_path(new_score.filepath)
    if old_portable != new_portable:
        _heal_setlist_paths({old_portable: new_portable})

        # Keep hash index in sync with in-app renames
        try:
            idx = load_hash_index(state.hash_index_path(), state.library_dir)
            if remap_hash_index(idx, {old_portable: new_portable}):
                save_hash_index(state.hash_index_path(), idx, state.library_dir)
        except SafeJSONError:
            pass

        try:
            _heal_recent_paths({old_portable: new_portable})
        except SafeJSONError:
            pass

    return {"ok": True, "score": new_score.to_dict(state.library_dir)}


@app.get("/api/library")
def get_library():
    """Every score in the library, in scan order.

    Clients filter, sort and build the composer/tag facets themselves
    (library.js applyFilters/searchScores), so one cached response serves
    every view, online or offline.
    """
    return {
        "scores": [s.to_dict(state.library_dir) for s in state.scores],
        "total": len(state.scores),
    }


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """True if an If-None-Match header names *etag* (weakly: a proxy that
    compresses may send it back as W/"...")."""
    if not if_none_match:
        return False
    tags = [t.strip().removeprefix("W/") for t in if_none_match.split(",")]
    return etag.removeprefix("W/") in tags


@app.get("/api/pdf")
def serve_pdf(request: Request, path: str = Query(..., description="Score filepath")):
    """The PDF, or 304 when the client's copy (If-None-Match) is current:
    the service worker revalidates every cached PDF each time it is viewed."""
    resolved = _validate_library_path(path)
    resp = FileResponse(
        resolved,
        media_type="application/pdf",
        filename=os.path.basename(resolved),
        headers={"Cache-Control": "no-cache"},
        stat_result=os.stat(resolved),
    )
    etag = resp.headers["etag"]
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304,
                        headers={"ETag": etag, "Cache-Control": "no-cache"})
    return resp


# ---------------------------------------------------------------------------
# Annotation endpoints
# ---------------------------------------------------------------------------


@app.get("/api/annotations")
def get_annotations(path: str = Query(..., description="PDF filepath")):
    resolved = _validate_library_path(path)
    try:
        data = load_annotations(resolved)
    except Exception:
        log.exception("Failed to load annotations for %s", resolved)
        raise HTTPException(status_code=500, detail="Failed to load annotations")
    return data


class SaveAnnotationsRequest(BaseModel):
    path: str
    pages: dict
    rotations: dict
    expected_etag: str | None = None


@app.put("/api/annotations")
def put_annotations(req: SaveAnnotationsRequest):
    resolved = _validate_library_path(req.path)
    try:
        new_etag = save_annotations(
            resolved, req.pages, req.rotations,
            expected_etag=req.expected_etag,
        )
    except AnnotationConflictError:
        raise HTTPException(
            status_code=409,
            detail="Annotations were modified by another session",
        )
    except SafeJSONError:
        log.exception("Failed to save annotations for %s", resolved)
        raise HTTPException(status_code=500, detail="Failed to save annotations")
    return {"ok": True, "etag": new_etag}


# ---------------------------------------------------------------------------
# Recent endpoints
# ---------------------------------------------------------------------------


MAX_RECENT = 50


def _load_recent() -> list[dict]:
    try:
        return load_recent(state.recent_path(), state.library_dir)
    except SafeJSONError:
        return []


def _save_recent(data: list[dict]) -> None:
    save_recent(state.recent_path(), data, state.library_dir)


class AddRecentRequest(BaseModel):
    path: str


@app.get("/api/recent")
def get_recent():
    """Recent entries enriched with the score's current tags.

    Tags are looked up live from the scanned library (matched by path)
    rather than stored in the recent file, so renamed/retagged scores stay
    accurate. Entries no longer in the library get an empty tag list.
    """
    recent = _load_recent()
    for entry in recent:
        fp = entry.get("filepath")
        score = state.find_score(fp) if isinstance(fp, str) and fp else None
        entry["tags"] = sorted(score.tags) if score else []
    map_recent_paths(recent, _api_path)
    return {"recent": recent}


@app.get("/api/newest")
def get_newest(limit: int = Query(20, ge=1, le=200)):
    """The most recently modified PDFs, newest first.

    Sorted by file mtime captured at scan time. Returns the same score
    shape as ``/api/library`` so the client can render tags and the
    offline-cache control identically.
    """
    newest = sorted(
        state.scores, key=lambda s: s.mtime, reverse=True
    )[:limit]
    return {
        "scores": [s.to_dict(state.library_dir) for s in newest],
        "total": len(newest),
    }


@app.post("/api/recent")
def add_recent(req: AddRecentRequest):
    resolved = _validate_library_path(req.path)
    pkey = portable_path(resolved)
    score = state.find_score(resolved)
    if score is None:
        raise HTTPException(status_code=404, detail="Score not in library")
    data = _load_recent()
    data = [e for e in data if e.get("filepath") != pkey]
    data.insert(0, {
        "filepath": pkey,
        "composer": score.composer,
        "title": score.title,
        "content_hash": score.content_hash or "",
        "timestamp": int(time.time() * 1000),
    })
    if len(data) > MAX_RECENT:
        data = data[:MAX_RECENT]
    _save_recent(data)
    return {"ok": True, "count": len(data)}


@app.delete("/api/recent")
def clear_recent():
    _save_recent([])
    return {"ok": True}


# ---------------------------------------------------------------------------
# Setlist endpoints
# ---------------------------------------------------------------------------


def _load_setlists() -> dict:
    try:
        return load_setlists(state.setlist_path(), state.library_dir)
    except SafeJSONError:
        return {}


def _save_setlists(data: dict) -> None:
    save_setlists(state.setlist_path(), data, state.library_dir)


_MAX_NESTING_DEPTH = 10


def _normalize_items(items: list[dict]) -> list[dict]:
    """Ensure every item has a 'type' field. Legacy items get type='song'.

    Returns a new list; input dicts without 'type' are shallow-copied
    to avoid mutating the on-disk data structure.
    """
    result = []
    for item in items:
        if "type" not in item:
            result.append({**item, "type": "song"})
        else:
            result.append(item)
    return result


def _validate_setlist_items(items: list[dict]) -> None:
    """Validate item types and required fields. Raises 400 on bad data."""
    for item in items:
        item_type = item.get("type", "song")
        if item_type == "song":
            # Songs are loosely validated — frontend controls the shape
            pass
        elif item_type == "setlist_ref":
            ref_name = item.get("setlist_name", "")
            if not isinstance(ref_name, str) or not ref_name.strip():
                raise HTTPException(
                    status_code=400,
                    detail="setlist_ref requires a non-empty setlist_name",
                )
        else:
            raise HTTPException(
                status_code=400, detail=f"Unknown item type: {item_type}"
            )


def _detect_cycle(
    data: dict, setlist_name: str, items: list[dict],
    visited: set[str] | None = None,
) -> bool:
    """Return True if items (or transitive sub-setlist refs) reference setlist_name."""
    if visited is None:
        visited = {setlist_name}
    for item in items:
        if item.get("type") == "setlist_ref":
            ref = item["setlist_name"]
            if ref in visited:
                return True
            ref_items = data.get(ref, {}).get("items", [])
            if _detect_cycle(data, setlist_name, ref_items, visited | {ref}):
                return True
    return False


def _expand_setlist(
    data: dict, name: str, rng=None,
    _expanding: frozenset[str] | None = None, _depth: int = 0,
) -> list[dict]:
    """Recursively expand setlist_ref items into a flat song list.

    With an *rng* (playback), a setlist flagged shuffle=True has its top-level
    items shuffled; each referenced setlist's own flag governs its expansion.
    Without one, order is preserved.
    """
    if _expanding is None:
        _expanding = frozenset()
    if name not in data or name in _expanding or _depth > _MAX_NESTING_DEPTH:
        return []
    sl = data[name]
    items = _normalize_items(list(sl["items"]))
    if rng is not None and sl.get("shuffle"):
        rng.shuffle(items)
    _expanding = _expanding | {name}
    result: list[dict] = []
    for item in items:
        if item.get("type") == "setlist_ref":
            result.extend(
                _expand_setlist(
                    data, item["setlist_name"], rng, _expanding, _depth + 1,
                )
            )
        else:
            result.append(item)
    return result


@app.get("/api/setlists")
def get_setlists():
    data = _load_setlists()
    result = []
    for name, sl in sorted(data.items()):
        flat = _expand_setlist(data, name)
        result.append({
            "name": name,
            "count": len(sl["items"]),
            "flat_count": len(flat),
            "shuffle": bool(sl.get("shuffle", False)),
        })
    return {"setlists": result}


@app.get("/api/setlists/{name}")
def get_setlist(name: str):
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    sl = data[name]
    items = _normalize_items(list(sl["items"]))
    enriched: list[dict] = []
    for item in items:
        if item.get("type") == "setlist_ref":
            ref_name = item["setlist_name"]
            exists = ref_name in data
            flat = _expand_setlist(data, ref_name) if exists else []
            enriched.append({**item, "exists": exists, "flat_count": len(flat)})
        else:
            enriched.append(item)
    return {
        "name": name,
        "items": _api_songs(enriched),
        "shuffle": bool(sl.get("shuffle", False)),
    }


@app.get("/api/setlists/{name}/flat")
def get_setlist_flat(name: str):
    """Return the fully flattened song list (all setlist_refs expanded)."""
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    songs = _expand_setlist(data, name)
    return {"name": name, "songs": _api_songs(songs)}


@app.get("/api/setlists/{name}/playback")
def get_setlist_playback(name: str):
    """Return song list with shuffle applied recursively per shuffle flags.

    Each call randomizes fresh; subsequent calls return a different order
    when any setlist in the chain has shuffle=True.
    """
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    songs = _expand_setlist(data, name, random.Random())
    return {"name": name, "songs": _api_songs(songs)}


class CreateSetlistRequest(BaseModel):
    name: str


@app.post("/api/setlists")
def create_setlist(req: CreateSetlistRequest):
    name = _validate_setlist_name(req.name)
    data = _load_setlists()
    if name in data:
        raise HTTPException(status_code=409, detail="Setlist already exists")
    data[name] = {"items": [], "shuffle": False}
    _save_setlists(data)
    return {"ok": True, "name": name}


class UpdateSetlistItemsRequest(BaseModel):
    items: list[dict] | None = None
    # Backward compat: accept "songs" as an alias for "items"
    songs: list[dict] | None = None

    def resolved_items(self) -> list[dict]:
        return self.items if self.items is not None else (self.songs or [])


@app.put("/api/setlists/{name}")
def update_setlist(name: str, req: UpdateSetlistItemsRequest):
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    items = _normalize_items(req.resolved_items())
    _validate_setlist_items(items)
    if _detect_cycle(data, name, items):
        raise HTTPException(
            status_code=400, detail="Circular setlist reference detected"
        )
    data[name]["items"] = items
    _save_setlists(data)
    return {"ok": True}


class SetShuffleRequest(BaseModel):
    shuffle: bool


@app.post("/api/setlists/{name}/shuffle")
def set_setlist_shuffle(name: str, req: SetShuffleRequest):
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    data[name]["shuffle"] = bool(req.shuffle)
    _save_setlists(data)
    return {"ok": True, "shuffle": data[name]["shuffle"]}


@app.delete("/api/setlists/{name}")
def delete_setlist(name: str):
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    del data[name]
    _save_setlists(data)
    return {"ok": True}


class RenameSetlistRequest(BaseModel):
    new_name: str


@app.post("/api/setlists/{name}/rename")
def rename_setlist(name: str, req: RenameSetlistRequest):
    new_name = _validate_setlist_name(req.new_name)
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    if new_name in data:
        raise HTTPException(status_code=409, detail="Target name already exists")
    new_data = {}
    for k, v in data.items():
        new_data[new_name if k == name else k] = v
    # Cascade: update setlist_ref items in all setlists that reference old name
    for sl in new_data.values():
        for item in sl["items"]:
            if item.get("type") == "setlist_ref" and item.get("setlist_name") == name:
                item["setlist_name"] = new_name
    _save_setlists(new_data)
    return {"ok": True, "name": new_name}


# ---------------------------------------------------------------------------
# Serve the frontend
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.server:app", host="0.0.0.0", port=8989, reload=True)
