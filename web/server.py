"""
Folio — Web backend (FastAPI).

Run with:
    uvicorn web.server:app --reload
or:
    python -m web.server
"""

import datetime
import hashlib
import hmac
import logging
import os
import random
import re
import secrets
import shutil
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import (
    AnnotationConflictError,
    BAKED_TAG,
    SafeJSON,
    SafeJSONError,
    Score,
    annotation_sidecar_path,
    bake_score,
    export_annotated_pdf,
    load_annotations,
    normalize_path,
    pdf_page_count,
    portable_path,
    rename_score_tags,
    save_annotations,
    scan_library,
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


def _load_config() -> dict:
    try:
        data = SafeJSON.load(WEB_CONFIG_PATH, default={})
    except SafeJSONError:
        data = {}
    merged = {**DEFAULT_WEB_CONFIG, **data}
    return merged


def _save_config(cfg: dict) -> None:
    SafeJSON.save(WEB_CONFIG_PATH, cfg)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

_SESSION_COOKIE = "folio_session"
_SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days


def _get_auth_salt() -> str:
    """Return the auth salt.  Env var takes precedence, then config file.

    Reads the config file directly (not the in-memory cache) so that
    changes to auth_salt take effect without restarting the server.
    """
    env_salt = os.environ.get("FOLIO_AUTH_SALT", "").strip()
    if env_salt:
        return env_salt
    try:
        cfg = SafeJSON.load(WEB_CONFIG_PATH, default={})
        return cfg.get("auth_salt", "").strip()
    except SafeJSONError:
        return ""


def _get_session_secret() -> str:
    """Return (and auto-generate on first use) a persistent signing key."""
    secret = state.config.get("session_secret", "")
    if not secret:
        secret = secrets.token_hex(32)
        state.config["session_secret"] = secret
        _save_config(state.config)
    return secret


def _expected_passphrase(salt: str) -> str:
    today = datetime.date.today().isoformat()  # YYYY-MM-DD
    return f"{today}-{salt}"


def _make_session_token() -> str:
    """Create an HMAC-signed session token embedding a timestamp.

    The current auth salt is mixed into the HMAC so that changing the
    salt automatically invalidates all existing sessions.
    """
    salt = _get_auth_salt()
    ts = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp()))
    msg = f"{ts}.{salt}".encode()
    sig = hmac.new(
        _get_session_secret().encode(), msg, hashlib.sha256
    ).hexdigest()
    return f"{ts}.{sig}"


def _verify_session_token(token: str) -> bool:
    """Verify the token signature and check it's not expired."""
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    ts_str, sig = parts
    salt = _get_auth_salt()
    msg = f"{ts_str}.{salt}".encode()
    expected = hmac.new(
        _get_session_secret().encode(), msg, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    age = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) - ts
    return 0 <= age <= _SESSION_MAX_AGE


class AppState:
    """Mutable server-wide state."""

    def __init__(self) -> None:
        self.config: dict = _load_config()
        self.library_dir: str = ""
        self.scores: list[Score] = []

    def set_library(self, path: str) -> None:
        path = normalize_path(path)
        self.library_dir = path
        cache_path = self.scan_cache_path()
        try:
            hash_cache = SafeJSON.load(cache_path, default={})
            if not isinstance(hash_cache, dict):
                hash_cache = {}
        except SafeJSONError as e:
            log.warning("Could not load scan cache: %s", e)
            hash_cache = {}
        self.scores = scan_library(path, hash_cache=hash_cache)
        try:
            SafeJSON.save(cache_path, hash_cache)
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
            return os.path.join(self.library_dir, "setlists.json")
        return os.path.join(CONFIG_DIR, "setlists.json")

    def hash_index_path(self) -> str:
        return os.path.join(self.library_dir, "_hash_index.json")

    def scan_cache_path(self) -> str:
        return os.path.join(self.library_dir, "_scan_cache.json")

    def recent_path(self) -> str:
        if self.library_dir:
            return os.path.join(self.library_dir, "_recent.json")
        return os.path.join(CONFIG_DIR, "_recent.json")


def _heal_references(st: "AppState") -> None:
    """Compare content hashes against the previous index to detect renames.

    Heals setlist paths and annotation sidecars, then saves the new index.
    """
    index_path = st.hash_index_path()

    # Build new index: hash -> portable path
    new_index: dict[str, str] = {}
    for s in st.scores:
        if s.content_hash:
            new_index[s.content_hash] = portable_path(s.filepath)

    # Load previous index
    try:
        old_index = SafeJSON.load(index_path, default={})
    except SafeJSONError:
        old_index = {}

    # Detect renames: same hash, different path
    remap: dict[str, str] = {}
    new_paths = set(new_index.values())
    for h, old_path in old_index.items():
        if h in new_index and new_index[h] != old_path:
            # Only remap if the old path no longer exists in the library
            if old_path not in new_paths:
                remap[old_path] = new_index[h]

    if remap:
        log.info("Detected %d renamed score(s), healing references", len(remap))
        _heal_annotation_sidecars(remap)
        _heal_setlist_paths(st, remap)
        _heal_recent_paths(st, remap)

    # Save new index
    try:
        SafeJSON.save(index_path, new_index)
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


def _heal_setlist_paths(st: "AppState", remap: dict[str, str]) -> None:
    """Update setlist song paths that were renamed externally."""
    data = _load_setlists()
    changed = False
    for sl in data.values():
        for item in sl["items"]:
            if item.get("type", "song") != "song":
                continue
            old = item.get("path", "")
            if old in remap:
                item["path"] = remap[old]
                changed = True
    if changed:
        _save_setlists(data)


def _heal_recent_paths(st: "AppState", remap: dict[str, str]) -> None:
    """Update recent-list filepaths that were renamed externally."""
    data = _load_recent()
    changed = False
    for entry in data:
        old = entry.get("filepath", "")
        if old in remap:
            entry["filepath"] = remap[old]
            changed = True
    if changed:
        _save_recent(data)


state = AppState()

# Auto-load last directory on startup
_last = state.config.get("last_directory", "")
if _last:
    _resolved = normalize_path(_last)
    if os.path.isdir(_resolved):
        try:
            state.set_library(_resolved)
        except Exception as e:
            log.warning(f"Could not auto-load library {_resolved}: {e}")

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
    yield


app = FastAPI(
    title="Folio", version="2.8.26",
    docs_url=None, redoc_url=None, lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Security: rate limiting
# ---------------------------------------------------------------------------

_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 5.0  # seconds
_RATE_LIMIT = 30  # max requests per window per key

# Stricter limits for login: 5 attempts per 60 seconds per IP
_LOGIN_WINDOW = 60.0
_LOGIN_LIMIT = 5
_login_buckets: dict[str, list[float]] = defaultdict(list)

# Lockout after repeated failures: 15 failures in 5 min → 15 min lockout
_LOCKOUT_THRESHOLD = 15
_LOCKOUT_WINDOW = 300.0  # 5 minutes
_LOCKOUT_DURATION = 900.0  # 15 minutes
_login_failures: dict[str, list[float]] = defaultdict(list)
_lockouts: dict[str, float] = {}


def _check_rate_limit(key: str) -> None:
    now = time.monotonic()
    bucket = _rate_buckets[key]
    _rate_buckets[key] = bucket = [t for t in bucket if now - t < _RATE_WINDOW]
    if len(bucket) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests")
    bucket.append(now)


def _check_login_rate(client_ip: str) -> None:
    """Rate-limit and lockout check for login attempts."""
    now = time.monotonic()

    # Check lockout first
    lockout_until = _lockouts.get(client_ip, 0)
    if now < lockout_until:
        remaining = int(lockout_until - now)
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {remaining}s",
        )

    # Sliding window rate limit
    bucket = _login_buckets[client_ip]
    _login_buckets[client_ip] = bucket = [
        t for t in bucket if now - t < _LOGIN_WINDOW
    ]
    if len(bucket) >= _LOGIN_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Wait 60 seconds",
        )
    bucket.append(now)


def _record_login_failure(client_ip: str) -> None:
    """Track failed login and trigger lockout if threshold exceeded."""
    now = time.monotonic()
    failures = _login_failures[client_ip]
    _login_failures[client_ip] = failures = [
        t for t in failures if now - t < _LOCKOUT_WINDOW
    ]
    failures.append(now)
    if len(failures) >= _LOCKOUT_THRESHOLD:
        _lockouts[client_ip] = now + _LOCKOUT_DURATION
        log.warning("Login lockout triggered for %s", client_ip)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    client = request.client.host if request.client else "unknown"

    # Login gets its own stricter rate limit + lockout
    if path == "/api/login" and request.method == "POST":
        _check_login_rate(client)

    # Rate-limit write endpoints (login already checked above, but general
    # limit still applies to prevent abuse of other write endpoints)
    if request.method in ("POST", "PUT", "DELETE"):
        _check_rate_limit(f"{client}:{path}")

    # Auth check: protect /api/* except /api/login and /api/auth-status
    salt = _get_auth_salt()
    if salt and path.startswith("/api/") \
            and path not in ("/api/login", "/api/auth-status"):
        token = request.cookies.get(_SESSION_COOKIE, "")
        if not _verify_session_token(token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
            )

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
# Auth endpoints
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    passphrase: str


@app.get("/api/auth-status")
def auth_status(request: Request):
    """Check whether auth is enabled and whether the current session is valid."""
    salt = _get_auth_salt()
    if not salt:
        return {"auth_required": False, "authenticated": True}
    token = request.cookies.get(_SESSION_COOKIE, "")
    return {
        "auth_required": True,
        "authenticated": _verify_session_token(token),
    }


@app.post("/api/login")
def login(req: LoginRequest, request: Request):
    salt = _get_auth_salt()
    if not salt:
        return {"ok": True}

    client_ip = request.client.host if request.client else "unknown"

    if req.passphrase.strip() != _expected_passphrase(salt):
        _record_login_failure(client_ip)
        raise HTTPException(status_code=403, detail="Invalid passphrase")

    # Successful login — clear failure tracking for this IP
    _login_failures.pop(client_ip, None)
    _lockouts.pop(client_ip, None)

    token = _make_session_token()
    response = JSONResponse(content={"ok": True})
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
    )
    return response


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _resolve_under_library(filepath: str) -> str:
    """Resolve *filepath* and verify it is under the library root.

    Returns the normalised absolute path.  Raises 400 if no library is set,
    403 on traversal attempts.  Does NOT check whether the file exists.
    """
    if not state.library_dir:
        raise HTTPException(status_code=400, detail="No library directory set")
    resolved = os.path.realpath(normalize_path(filepath))
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


@app.put("/api/scores/tags")
def update_score_tags(req: UpdateTagsRequest):
    """Update the filename tags on a score, renaming the file on disk."""
    if not state.library_dir:
        raise HTTPException(status_code=400, detail="No library directory set")
    resolved = _validate_library_path(req.path)

    # Find the score in our in-memory library
    score = None
    score_idx = None
    for i, s in enumerate(state.scores):
        if os.path.normpath(s.filepath) == os.path.normpath(resolved):
            score = s
            score_idx = i
            break
    if score is None:
        raise HTTPException(status_code=404, detail="Score not found in library")

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
    state.scores[score_idx] = new_score

    # Update setlist references that point to the old path
    old_portable = portable_path(score.filepath)
    new_portable = portable_path(new_score.filepath)
    if old_portable != new_portable:
        data = _load_setlists()
        changed = False
        for sl in data.values():
            for item in sl["items"]:
                if item.get("type", "song") == "song" and item.get("path") == old_portable:
                    item["path"] = new_portable
                    changed = True
        if changed:
            _save_setlists(data)

        # Keep hash index in sync with in-app renames
        try:
            idx = SafeJSON.load(state.hash_index_path(), default={})
            if new_score.content_hash in idx:
                idx[new_score.content_hash] = new_portable
                SafeJSON.save(state.hash_index_path(), idx)
        except SafeJSONError:
            pass

        # Update recent-list entries that point to the old path
        recent = _load_recent()
        r_changed = False
        for entry in recent:
            if entry.get("filepath") == old_portable:
                entry["filepath"] = new_portable
                r_changed = True
        if r_changed:
            try:
                _save_recent(recent)
            except SafeJSONError:
                pass

    return {"ok": True, "score": new_score.to_dict()}


@app.get("/api/library")
def get_library(
    q: str = Query("", description="Text search (title or composer)"),
    composer: str = Query("", description="Exact composer filter"),
    tag: list[str] = Query([], description="Required tags"),
    sort: str = Query("composer", description="Sort column: composer, title, tags"),
    desc: bool = Query(False, description="Sort descending"),
):
    q_lower = q.lower()
    tag_set = {t.lower() for t in tag}

    matches = [
        s for s in state.scores
        if (not q_lower or q_lower in s.title.lower() or q_lower in s.composer.lower())
        and (not composer or s.composer == composer)
        and tag_set.issubset(s.tags)
    ]

    key_map = {
        "composer": lambda s: (s.composer.lower(), s.title.lower()),
        "title": lambda s: (s.title.lower(), s.composer.lower()),
        "tags": lambda s: (sorted(s.tags), s.composer.lower()),
    }
    if sort in key_map:
        matches.sort(key=key_map[sort], reverse=desc)

    # Gather available filter values (context-sensitive)
    all_composers: set[str] = set()
    all_tags: set[str] = set()
    for s in state.scores:
        title_match = (
            not q_lower
            or q_lower in s.title.lower()
            or q_lower in s.composer.lower()
        )
        if title_match and tag_set.issubset(s.tags):
            all_composers.add(s.composer)
        if title_match and (not composer or s.composer == composer) and tag_set.issubset(s.tags):
            all_tags.update(s.tags)

    return {
        "scores": [s.to_dict() for s in matches],
        "total": len(matches),
        "composers": sorted(all_composers),
        "tags": sorted(all_tags),
    }


@app.get("/api/pdf")
def serve_pdf(path: str = Query(..., description="Score filepath")):
    resolved = _validate_library_path(path)
    return FileResponse(
        resolved,
        media_type="application/pdf",
        filename=os.path.basename(resolved),
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/pdf/pages")
def pdf_pages(path: str = Query(..., description="Score filepath")):
    resolved = _validate_library_path(path)
    try:
        count = pdf_page_count(resolved)
    except Exception:
        log.exception("Page count failed for %s", resolved)
        raise HTTPException(status_code=500, detail="Failed to read PDF")
    return {"path": portable_path(path), "pages": count}


@app.get("/api/pdf/export")
def export_pdf(path: str = Query(..., description="Score filepath")):
    """Download a copy of the PDF with annotations baked in."""
    resolved = _validate_library_path(path)
    try:
        pdf_bytes = export_annotated_pdf(resolved)
    except Exception:
        log.exception("PDF export failed for %s", resolved)
        raise HTTPException(status_code=500, detail="Export failed")
    basename = re.sub(r'[^a-zA-Z0-9_.\- ]', '_', os.path.basename(resolved))
    filename = f"annotated_{basename}"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class BakeRequest(BaseModel):
    path: str


@app.post("/api/pdf/bake")
def bake_pdf(req: BakeRequest):
    """Write a sibling PDF with annotations flattened in.

    The baked file gets the ``baked`` filename tag. Original PDF and
    its sidecar are untouched; no sidecar is created for the baked file.
    Already-baked scores are rejected.
    """
    if not state.library_dir:
        raise HTTPException(status_code=400, detail="No library directory set")
    resolved = _validate_library_path(req.path)

    score = None
    for s in state.scores:
        if os.path.normpath(s.filepath) == os.path.normpath(resolved):
            score = s
            break
    if score is None:
        raise HTTPException(status_code=404, detail="Score not found in library")

    if BAKED_TAG in score.filename_tags:
        raise HTTPException(status_code=409, detail="Score is already baked")

    try:
        new_score = bake_score(score)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception:
        log.exception("Bake failed for %s", resolved)
        raise HTTPException(status_code=500, detail="Bake failed")

    state.scores.append(new_score)

    if new_score.content_hash:
        try:
            idx = SafeJSON.load(state.hash_index_path(), default={})
            if isinstance(idx, dict):
                idx[new_score.content_hash] = portable_path(new_score.filepath)
                SafeJSON.save(state.hash_index_path(), idx)
        except SafeJSONError:
            log.warning("Could not update hash index after bake")

    return {
        "path": portable_path(new_score.filepath),
        "filename": new_score.filename,
    }


# ---------------------------------------------------------------------------
# Annotation endpoints
# ---------------------------------------------------------------------------


def _validate_pdf_in_library(filepath: str) -> str:
    """Like _validate_library_path but allows the file to not exist yet
    (annotations can be created before the sidecar JSON exists)."""
    return _resolve_under_library(filepath)


@app.get("/api/annotations")
def get_annotations(path: str = Query(..., description="PDF filepath")):
    resolved = _validate_pdf_in_library(path)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="PDF not found")
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
    resolved = _validate_pdf_in_library(req.path)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="PDF not found")
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
        data = SafeJSON.load(state.recent_path(), default=[])
    except SafeJSONError:
        return []
    return data if isinstance(data, list) else []


def _save_recent(data: list[dict]) -> None:
    SafeJSON.save(state.recent_path(), data)


class AddRecentRequest(BaseModel):
    path: str


@app.get("/api/recent")
def get_recent():
    """Recent entries enriched with the score's current tags.

    Tags are looked up live from the scanned library (matched by portable
    path) rather than stored in the recent file, so renamed/retagged scores
    stay accurate. Entries no longer in the library get an empty tag list.
    """
    by_path = {portable_path(s.filepath): s for s in state.scores}
    recent = _load_recent()
    for entry in recent:
        score = by_path.get(entry.get("filepath"))
        entry["tags"] = sorted(score.tags) if score else []
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
        "scores": [s.to_dict() for s in newest],
        "total": len(newest),
    }


@app.post("/api/recent")
def add_recent(req: AddRecentRequest):
    if not state.library_dir:
        raise HTTPException(status_code=400, detail="No library directory set")
    resolved = _validate_library_path(req.path)
    pkey = portable_path(resolved)
    score = next(
        (s for s in state.scores
         if portable_path(s.filepath) == pkey),
        None,
    )
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


def _normalize_setlist_value(v) -> dict:
    """Normalize a stored setlist value to {"items": [...], "shuffle": bool}.

    Older versions stored each setlist as a bare list of items; this lifts
    them into the new dict form transparently on load.
    """
    if isinstance(v, list):
        return {"items": v, "shuffle": False}
    if isinstance(v, dict):
        items = v.get("items")
        if not isinstance(items, list):
            items = []
        return {"items": items, "shuffle": bool(v.get("shuffle", False))}
    return {"items": [], "shuffle": False}


def _load_setlists() -> dict:
    try:
        raw = SafeJSON.load(state.setlist_path(), default={})
    except SafeJSONError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {name: _normalize_setlist_value(v) for name, v in raw.items()}


def _save_setlists(data: dict) -> None:
    SafeJSON.save(state.setlist_path(), data)


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


def _flatten_setlist(
    data: dict, name: str, _expanding: frozenset[str] | None = None,
    _depth: int = 0,
) -> list[dict]:
    """Recursively expand setlist_ref items into a flat song list."""
    if _expanding is None:
        _expanding = frozenset()
    if name not in data or name in _expanding or _depth > _MAX_NESTING_DEPTH:
        return []
    _expanding = _expanding | {name}
    result: list[dict] = []
    for item in _normalize_items(list(data[name]["items"])):
        if item.get("type") == "setlist_ref":
            result.extend(
                _flatten_setlist(
                    data, item["setlist_name"], _expanding, _depth + 1,
                )
            )
        else:
            result.append(item)
    return result


def _expand_for_playback(
    data: dict, name: str, rng,
    _expanding: frozenset[str] | None = None, _depth: int = 0,
) -> list[dict]:
    """Recursively expand for playback, shuffling per-setlist when flagged.

    Semantics:
      * If this setlist has shuffle=True, its top-level items are shuffled.
      * setlist_ref items are recursively expanded; the referenced setlist's
        own shuffle flag governs its expansion.
    """
    if _expanding is None:
        _expanding = frozenset()
    if name not in data or name in _expanding or _depth > _MAX_NESTING_DEPTH:
        return []
    sl = data[name]
    items = _normalize_items(list(sl["items"]))
    if sl.get("shuffle"):
        rng.shuffle(items)
    _expanding = _expanding | {name}
    result: list[dict] = []
    for item in items:
        if item.get("type") == "setlist_ref":
            result.extend(
                _expand_for_playback(
                    data, item["setlist_name"], rng,
                    _expanding, _depth + 1,
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
        flat = _flatten_setlist(data, name)
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
            flat = _flatten_setlist(data, ref_name) if exists else []
            enriched.append({**item, "exists": exists, "flat_count": len(flat)})
        else:
            enriched.append(item)
    return {
        "name": name,
        "items": enriched,
        "shuffle": bool(sl.get("shuffle", False)),
    }


@app.get("/api/setlists/{name}/flat")
def get_setlist_flat(name: str):
    """Return the fully flattened song list (all setlist_refs expanded)."""
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    songs = _flatten_setlist(data, name)
    return {"name": name, "songs": songs}


@app.get("/api/setlists/{name}/playback")
def get_setlist_playback(name: str):
    """Return song list with shuffle applied recursively per shuffle flags.

    Each call randomizes fresh; subsequent calls return a different order
    when any setlist in the chain has shuffle=True.
    """
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    songs = _expand_for_playback(data, name, random.Random())
    return {"name": name, "songs": songs}


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
