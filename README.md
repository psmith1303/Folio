# Folio

A web application to view, navigate, and annotate PDF music scores.
Runs on any device with a browser, including iPad.

## Features
- PDF viewing with three display modes: Fit (single page), Wide (full width, scroll vertically), and 2-up (side-by-side)
- Annotations: pen (freehand ink), text, eraser, with per-page undo
- 7-colour palette, adjustable pen/text size, musical symbol shortcuts
- Touch and Apple Pencil support (Pointer Events API)
- Metadata search by composer, title, and folder tags
- Add scores to setlists directly from the viewer (`s` key)
- Click-to-navigate in Fit/2-up modes: right/bottom half = next page, left/top half = previous
- Wide mode: scroll vertically, arrow/space keys scroll natively; page turns via toolbar, PageUp/PageDown, or scroll-boundary in fullscreen
- Keyboard shortcuts for page navigation and tool switching
- Setlist management: create, edit, reorder, rename, delete, playback with page constraints
- Nested setlists: setlists can reference other setlists as sub-items, with automatic flattening for playback
- Dark/light theme toggle (remembered across sessions)
- Export annotated PDF with annotations baked in
- Fullscreen mode for distraction-free viewing (f key or toolbar button)
- Directories with a `.exclude` file are hidden from the library

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

## Authentication

When exposed to the internet, enable authentication by setting an auth salt.
The passphrase is `YYYY-MM-DD-<salt>` (today's date + your salt), so it changes
daily and requires no memorisation.

### Option 1: environment variable
```
FOLIO_AUTH_SALT=psmith python3 -m uvicorn web.server:app --host 0.0.0.0 --port 8989
```

### Option 2: config file
Add `"auth_salt": "psmith"` to `~/.folio/web_config.json`.

Once authenticated, a 30-day session cookie is set — no need to re-enter
the passphrase on every visit.  With no salt configured, auth is disabled.

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
| `tests/test_web_core.py` | 36 | `web.core` module: path utils, SafeJSON, Score parsing, library scanning (.exclude support), annotation load/save/migration, etag, conflict detection |
| `tests/test_web_api.py` | 72 | FastAPI endpoints: config, library, PDF serving, annotation CRUD, rotation, etag/conflict, setlist CRUD/rename, nested setlists (refs, flattening, cycle detection, rename cascading, backward compat), PDF export, path traversal, security, auth |

## Emacs Editing

`setlist-editor.el` lets you edit `setlists.json` in Emacs without touching
raw JSON.  Each setlist becomes an org level-1 heading; each song is a table
row.  Setlist references (nested setlists) appear as `>>Name` in the Title
column.  Requires Emacs 27+; no external packages needed.

### Setup

```elisp
;; In your Emacs init file, or load manually with M-x load-file:
(load "/path/to/Folio/setlist-editor.el")
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
| `web/server.py` | FastAPI application — library browsing, PDF serving, annotation CRUD, setlist CRUD with nested references, config endpoints. |

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
| Space, n, →, ↓, PgDn | Next page (in Wide mode, ↓/↑/Space scroll natively) |
| Backspace, p, ←, ↑, PgUp | Previous page |
| Home / End | First / last page |
| Escape | Back to library (or exit fullscreen) |
| v / d / t / e | Nav / Pen / Text / Eraser tool |
| s | Add current score to a setlist |
| f | Toggle fullscreen |
| r / R | Rotate page CW / CCW |
| Ctrl+Z | Undo |
