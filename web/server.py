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
import re
import secrets
import time
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from .core import (
    AnnotationConflictError,
    SafeJSON,
    SafeJSONError,
    Score,
    build_tagged_filename,
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

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

DEFAULT_WEB_CONFIG = {
    "last_directory": "",
    "allowed_roots": [],
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
        self.scores = scan_library(path)
        self.config["last_directory"] = portable_path(path)
        _save_config(self.config)
        logging.info(
            f"Library set to {path} — {len(self.scores)} scores found"
        )

    def setlist_path(self) -> str:
        if self.library_dir:
            return os.path.join(self.library_dir, "setlists.json")
        return os.path.join(CONFIG_DIR, "setlists.json")


state = AppState()

# Auto-load last directory on startup
_last = state.config.get("last_directory", "")
if _last:
    _resolved = normalize_path(_last)
    if os.path.isdir(_resolved):
        try:
            state.set_library(_resolved)
        except Exception as e:
            logging.warning(f"Could not auto-load library {_resolved}: {e}")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Folio", version="2.3.0", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _log_startup():
    logging.info("Folio v%s starting", app.version)


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
        logging.warning("Login lockout triggered for %s", client_ip)


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
        "script-src 'self' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
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


@app.get("/api/config")
def get_config():
    return {
        "library_dir": portable_path(state.library_dir),
        "score_count": len(state.scores),
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
        for sl_items in data.values():
            for item in sl_items:
                if item.get("type", "song") == "song" and item.get("path") == old_portable:
                    item["path"] = new_portable
                    changed = True
        if changed:
            _save_setlists(data)

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
        logging.exception("Page count failed for %s", resolved)
        raise HTTPException(status_code=500, detail="Failed to read PDF")
    return {"path": portable_path(path), "pages": count}


@app.get("/api/pdf/export")
def export_pdf(path: str = Query(..., description="Score filepath")):
    """Download a copy of the PDF with annotations baked in."""
    resolved = _validate_library_path(path)
    try:
        pdf_bytes = export_annotated_pdf(resolved)
    except Exception:
        logging.exception("PDF export failed for %s", resolved)
        raise HTTPException(status_code=500, detail="Export failed")
    basename = re.sub(r'[^\w.\- ]', '_', os.path.basename(resolved))
    filename = f"annotated_{basename}"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
        logging.exception("Failed to load annotations for %s", resolved)
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
        logging.exception("Failed to save annotations for %s", resolved)
        raise HTTPException(status_code=500, detail="Failed to save annotations")
    return {"ok": True, "etag": new_etag}


# ---------------------------------------------------------------------------
# Setlist endpoints
# ---------------------------------------------------------------------------


def _load_setlists() -> dict:
    try:
        return SafeJSON.load(state.setlist_path(), default={})
    except SafeJSONError:
        return {}


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
            ref_items = data.get(ref, [])
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
    for item in _normalize_items(list(data[name])):
        if item.get("type") == "setlist_ref":
            result.extend(
                _flatten_setlist(
                    data, item["setlist_name"], _expanding, _depth + 1,
                )
            )
        else:
            result.append(item)
    return result


@app.get("/api/setlists")
def get_setlists():
    data = _load_setlists()
    result = []
    for name, items in sorted(data.items()):
        flat = _flatten_setlist(data, name)
        result.append({
            "name": name,
            "count": len(items),
            "flat_count": len(flat),
        })
    return {"setlists": result}


@app.get("/api/setlists/{name}")
def get_setlist(name: str):
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    items = _normalize_items(list(data[name]))
    enriched: list[dict] = []
    for item in items:
        if item.get("type") == "setlist_ref":
            ref_name = item["setlist_name"]
            exists = ref_name in data
            flat = _flatten_setlist(data, ref_name) if exists else []
            enriched.append({**item, "exists": exists, "flat_count": len(flat)})
        else:
            enriched.append(item)
    return {"name": name, "items": enriched}


@app.get("/api/setlists/{name}/flat")
def get_setlist_flat(name: str):
    """Return the fully flattened song list (all setlist_refs expanded)."""
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    songs = _flatten_setlist(data, name)
    return {"name": name, "songs": songs}


class CreateSetlistRequest(BaseModel):
    name: str


@app.post("/api/setlists")
def create_setlist(req: CreateSetlistRequest):
    name = _validate_setlist_name(req.name)
    data = _load_setlists()
    if name in data:
        raise HTTPException(status_code=409, detail="Setlist already exists")
    data[name] = []
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
    data[name] = items
    _save_setlists(data)
    return {"ok": True}


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
    for items in new_data.values():
        for item in items:
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
