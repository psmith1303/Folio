# Music Score Viewer

A Python application to view, navigate, and annotate PDF music scores.

## Features
- Zoom-to-fit and side-by-side page view
- Per-page rotation (stored non-destructively in a sidecar file)
- Annotations: pen, text, eraser, with per-page undo
- Metadata search by composer, title, and folder tags
- Setlist management: create, reorder, and play through sets of scores
- Cross-platform: runs as a Windows executable, via Python on WSL/Linux, or as a web app (iPad/browser)

## Requirements
- Python 3.10+
- Dependencies listed in `requirements.txt`

## How to Run
1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Run:
   ```
   python MusicScoreViewer.py
   ```

## Running the Web App

The web version serves your music library over HTTP, accessible from any
browser (including iPad).

1. Install web dependencies (WSL / Debian / Ubuntu):
   ```
   sudo apt install python3-uvicorn python3-fastapi python3-pymupdf
   ```
2. Start the server:
   ```
   python3 -m uvicorn web.server:app --host 0.0.0.0 --port 8000
   ```
3. Open `http://<your-machine>:8000` in a browser or on your iPad.

On first launch, click **Set Folder** to point it at your music library
directory. The setting is remembered across restarts.

## Building the Windows Executable
Run `make.bat` from the project root. Requires PyInstaller and `icon.ico` to
be present in the same directory.

## Running the Tests

### Install test dependencies
```
pip install -r requirements-dev.txt
```

### Linux / WSL
```
python3 -m pytest -v
```

### Windows
```
python -m pytest -v
```

Some tests are platform-specific. On Linux/WSL, 4 Windows-only path tests
are skipped (102 of 106 pass). On Windows, 11 Linux/WSL-only tests are skipped
instead (95 of 106 pass).

### What the tests cover

| File | Total | Linux passes | Windows passes | What is tested |
|---|---|---|---|---|
| `tests/test_path_utils.py` | 22 | 18 | 12 | `normalize_path()` and `portable_path()`, including WSL↔Windows translation and round-trip invariants |
| `tests/test_rotation.py` | 17 | 17 | 17 | `_rotate_annotation_coords()` rotation transform maths: identity, known corners, CW/CCW inverse, composition, bounds |
| `tests/test_safe_json.py` | 21 | 21 | 20 | `SafeJSON.load()` and `SafeJSON.save()`: missing files, valid JSON, corrupt JSON, missing directory, cross-device/network-drive write, unicode, round-trips |
| `tests/test_web_core.py` | 27 | 27 | 27 | `web.core` module: path utils, SafeJSON (exception-based), Score parsing, library scanning, annotation load/save/migration |
| `tests/test_web_api.py` | 19 | 19 | 19 | FastAPI endpoints: config, library listing/filtering/sorting, PDF serving, annotation CRUD, path traversal protection |

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

The org buffer is ephemeral (never saved as a file). `C-x C-s` is intercepted
and redirected to the minibuffer hint. A blank **End** cell round-trips as
JSON `null` (meaning "last page of the PDF").

---

## Architecture

### Key classes and module-level constructs

| Name | Kind | Description |
|---|---|---|
| `SetlistSession` | dataclass | Holds all active setlist playback state (`name`, `items`, `index`, `start_page`, `end_page`). `None` on `MusicScoreApp._session` means library mode; set means setlist mode. |
| `_rotate_annotation_coords` | function | Rotates annotation coordinates in-place by N×90°. Used by `AnnotationManager` and tested directly in `tests/test_rotation.py`. |
| `AnnotationManager` | class | Owns annotation state (`annotations`, `rotations`, `_undo_stack`, `tool`, `pen_color`, `current_stroke`) and all persistence / mutation logic. Accessed via `app.annot`. |
| `MusicScoreApp` | class | Main Tk application controller. Delegates annotation work to `self.annot` and setlist state to `self._session`. |

### Web backend (`web/`)

| File | Description |
|---|---|
| `web/core.py` | Tk-free business logic: `SafeJSON` (raises exceptions instead of dialogs), `Score`, `scan_library()`, path utilities. Reused by the FastAPI server. |
| `web/server.py` | FastAPI application — library browsing, PDF serving, config endpoints. |
| `web/static/` | Vanilla JS frontend with pdf.js for client-side PDF rendering. |

### File formats

- **`setlists.json`** — setlist definitions, written to the root of the music library folder so setlists travel with the collection; see `docs/setlist-file-format.md` for the full specification.
- **`<score>.json`** — annotation sidecar written alongside each PDF; versioned JSON containing per-page annotation lists and rotation overrides.
