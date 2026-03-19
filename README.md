# Music Score Viewer

A web application to view, navigate, and annotate PDF music scores.
Runs on any device with a browser, including iPad.

## Features
- PDF viewing with zoom-to-fit and side-by-side page view
- Annotations: pen (freehand ink), text, eraser, with per-page undo
- 7-colour palette, adjustable pen/text size, musical symbol shortcuts
- Touch and Apple Pencil support (Pointer Events API)
- Metadata search by composer, title, and folder tags
- Click-to-navigate: right/bottom half = next page, left/top half = previous
- Keyboard shortcuts for page navigation and tool switching
- Setlist management: create, edit, reorder, rename, delete, playback with page constraints
- Dark/light theme toggle (remembered across sessions)
- Export annotated PDF with annotations baked in

## Requirements
- Python 3.10+
- Dependencies: `fastapi`, `uvicorn`, `pymupdf`

## How to Run

### WSL / Debian / Ubuntu (recommended)
```
sudo apt install python3-uvicorn python3-fastapi python3-pymupdf
```

### Or via pip
```
pip install -r requirements.txt
```

### Start the server
```
python3 -m uvicorn web.server:app --host 0.0.0.0 --port 8989
```

Open `http://<your-machine>:8989` in a browser or on your iPad.
On first launch, a dialog prompts for your music library path.
The setting is remembered across restarts.

## Running the Tests

### Install test dependencies
```
pip install -r requirements-dev.txt
```

### Run
```
python3 -m pytest -v
```

### What the tests cover

| File | Tests | What is tested |
|---|---|---|
| `tests/test_web_core.py` | 27 | `web.core` module: path utils, SafeJSON, Score parsing, library scanning, annotation load/save/migration |
| `tests/test_web_api.py` | 38 | FastAPI endpoints: config, library, PDF serving, annotation CRUD, rotation, setlist CRUD/rename, PDF export, path traversal |

## Emacs Editing

`setlist-editor.el` lets you edit `setlists.json` in Emacs without touching
raw JSON.  Each setlist becomes an org level-1 heading; each song is a table
row.  Requires Emacs 27+; no external packages needed.

### Setup

```elisp
;; In your Emacs init file, or load manually with M-x load-file:
(load "/path/to/MusicScoreViewer/setlist-editor.el")
```

### Usage

1. `M-x setlist-edit` — prompts for `setlists.json` and opens it as org tables.
2. Edit cells with standard org table commands:
   - **Tab** — move to the next cell (auto-aligns the row)
   - **C-c C-c** — re-align the current table
3. **C-c C-s** — write the tables back to JSON and save the file.
4. **C-c C-q** — quit (prompts if there are unsaved changes).

---

## Architecture

### Backend (`web/`)

| File | Description |
|---|---|
| `web/core.py` | Business logic: `SafeJSON`, `Score`, `scan_library()`, path utilities, annotation load/save with format migration. |
| `web/server.py` | FastAPI application — library browsing, PDF serving, annotation CRUD, config endpoints. |

### Frontend (`web/static/`)

| File | Description |
|---|---|
| `web/static/app.js` | ES module: pdf.js rendering, annotation canvas overlay (pen/text/eraser/undo), library UI, keyboard/touch navigation. |
| `web/static/app.css` | Dark/light theme, responsive layout, annotation toolbar styles. |
| `web/static/index.html` | Single-page app shell. |

### File formats

- **`setlists.json`** — setlist definitions, written to the root of the music library folder; see `docs/setlist-file-format.md` for the full specification.
- **`<score>.json`** — annotation sidecar written alongside each PDF; versioned JSON containing per-page annotation lists and rotation overrides.

### Keyboard shortcuts (score viewer)

| Key | Action |
|---|---|
| Space, n, →, ↓, PgDn | Next page |
| Backspace, p, ←, ↑, PgUp | Previous page |
| Home / End | First / last page |
| Escape | Back to library |
| v / d / t / e | Nav / Pen / Text / Eraser tool |
| r / R | Rotate page CW / CCW |
| Ctrl+Z | Undo |
