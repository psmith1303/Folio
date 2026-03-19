"""
MusicScoreViewer — Web backend (FastAPI).

Run with:
    uvicorn web.server:app --reload
or:
    python -m web.server
"""

import logging
import os
from pathlib import PurePosixPath

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import (
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

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".music_score_viewer")
os.makedirs(CONFIG_DIR, exist_ok=True)
WEB_CONFIG_PATH = os.path.join(CONFIG_DIR, "web_config.json")

DEFAULT_WEB_CONFIG = {
    "last_directory": "",
}


def _load_config() -> dict:
    try:
        data = SafeJSON.load(WEB_CONFIG_PATH, default={})
    except SafeJSONError:
        data = {}
    merged = {**DEFAULT_WEB_CONFIG, **data}
    return merged


def _save_config(cfg: dict) -> None:
    SafeJSON.save(WEB_CONFIG_PATH, cfg)


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

app = FastAPI(title="MusicScoreViewer", version="0.1.0")


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _validate_library_path(filepath: str) -> str:
    """Resolve *filepath* and verify it is under the library root.

    Returns the normalised absolute path.  Raises 403 on traversal attempts.
    """
    if not state.library_dir:
        raise HTTPException(status_code=400, detail="No library directory set")
    resolved = os.path.realpath(normalize_path(filepath))
    root = os.path.realpath(state.library_dir)
    if not resolved.startswith(root + os.sep) and resolved != root:
        raise HTTPException(status_code=403, detail="Path outside library")
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


class SetLibraryRequest(BaseModel):
    path: str


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
        raise HTTPException(status_code=404, detail=f"Directory not found: {path}")
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"path": portable_path(path), "pages": count}


@app.get("/api/pdf/export")
def export_pdf(path: str = Query(..., description="Score filepath")):
    """Download a copy of the PDF with annotations baked in."""
    resolved = _validate_library_path(path)
    try:
        pdf_bytes = export_annotated_pdf(resolved)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    filename = f"annotated_{os.path.basename(resolved)}"
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
    if not state.library_dir:
        raise HTTPException(status_code=400, detail="No library directory set")
    resolved = os.path.realpath(normalize_path(filepath))
    root = os.path.realpath(state.library_dir)
    if not resolved.startswith(root + os.sep) and resolved != root:
        raise HTTPException(status_code=403, detail="Path outside library")
    return resolved


@app.get("/api/annotations")
def get_annotations(path: str = Query(..., description="PDF filepath")):
    resolved = _validate_pdf_in_library(path)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="PDF not found")
    try:
        data = load_annotations(resolved)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return data


class SaveAnnotationsRequest(BaseModel):
    path: str
    pages: dict
    rotations: dict


@app.put("/api/annotations")
def put_annotations(req: SaveAnnotationsRequest):
    resolved = _validate_pdf_in_library(req.path)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="PDF not found")
    try:
        save_annotations(resolved, req.pages, req.rotations)
    except SafeJSONError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


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
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
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
    new_name = req.new_name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
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
