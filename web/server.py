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
    export_annotated_pdf,
    load_annotations,
    normalize_path,
    pdf_page_count,
    portable_path,
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

app = FastAPI(title="Folio", version="0.1.0", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Security: rate limiting (simple in-memory per-IP, per-endpoint bucket)
# ---------------------------------------------------------------------------

_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 5.0  # seconds
_RATE_LIMIT = 30  # max requests per window per key


def _check_rate_limit(key: str) -> None:
    now = time.monotonic()
    bucket = _rate_buckets[key]
    # Evict old entries
    _rate_buckets[key] = bucket = [t for t in bucket if now - t < _RATE_WINDOW]
    if len(bucket) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests")
    bucket.append(now)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path

    # Rate-limit write endpoints
    if request.method in ("POST", "PUT", "DELETE"):
        client = request.client.host if request.client else "unknown"
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
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
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
def login(req: LoginRequest):
    salt = _get_auth_salt()
    if not salt:
        return {"ok": True}
    if req.passphrase.strip() != _expected_passphrase(salt):
        raise HTTPException(status_code=403, detail="Invalid passphrase")
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


@app.get("/api/setlists")
def get_setlists():
    data = _load_setlists()
    return {
        "setlists": [
            {"name": name, "count": len(songs)}
            for name, songs in sorted(data.items())
        ],
    }


@app.get("/api/setlists/{name}")
def get_setlist(name: str):
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    return {"name": name, "songs": data[name]}


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


class UpdateSetlistSongsRequest(BaseModel):
    songs: list[dict]


@app.put("/api/setlists/{name}")
def update_setlist(name: str, req: UpdateSetlistSongsRequest):
    data = _load_setlists()
    if name not in data:
        raise HTTPException(status_code=404, detail="Setlist not found")
    data[name] = req.songs
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
