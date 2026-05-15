# Folio

A web application to view, navigate, and annotate PDF music scores.
Runs on any device with a browser, including iPad.

## Features
- PDF viewing with three display modes: Fit (single page), Wide (full width, scroll vertically), and 2-up (side-by-side)
- Annotations: pen (freehand ink), text, eraser, per-page undo, Clear Page (one-click erase of all annotations on the current page, with confirmation and undo)
- 7-colour palette, adjustable pen/text size, musical symbol shortcuts
- Touch and Apple Pencil support (Pointer Events API), with optional Pencil-only mode that ignores finger/mouse input on the pen tool for palm-rejection during writing
- Metadata search by composer, title, and folder tags
- Add scores to setlists directly from the viewer (`s` key)
- Click-to-navigate in Fit/2-up modes: right/bottom half = next page, left/top half = previous
- Wide mode: scroll vertically, arrow/space keys scroll natively; page turns via toolbar, PageUp/PageDown, or scroll-boundary in fullscreen
- Configurable keyboard shortcuts for navigation, tools, and view switching
- Content-hash based identity: renamed/moved PDFs are auto-detected on rescan, healing setlist references, annotation sidecars, and recent list entries
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
FOLIO_AUTH_SALT=xyzzy python3 -m uvicorn web.server:app --host 0.0.0.0 --port 8989
```

### Option 2: config file
Add `"auth_salt": "xyzzy"` to `~/.folio/web_config.json`.

Once authenticated, a 30-day session cookie is set — no need to re-enter
the passphrase on every visit.  With no salt configured, auth is disabled.

## Keyboard Shortcuts

All shortcuts are configurable via `~/.folio/web_config.json` (see below).

### Global (work from any view)

| Default | Action |
|---|---|
| Alt+L | Switch to Library view |
| Alt+S | Switch to Setlists view |
| Alt+R | Switch to Recent view |
| Ctrl+F | Focus search input |
| Ctrl+R | Reset filters and rescan library |

### Score viewer

| Default | Action |
|---|---|
| Space, n, →, ↓, PgDn | Next page (in Wide mode, ↓/↑/Space scroll natively) |
| Backspace, p, ←, ↑, PgUp | Previous page |
| Home / End | First / last page |
| Escape | Back to library (or exit fullscreen) |
| v / d / t / e | Nav / Pen / Text / Eraser tool |
| s | Add current score to a setlist |
| g | Edit tags |
| f | Toggle fullscreen |
| r / Shift+R | Rotate page CW / CCW |
| Ctrl+Z | Undo |

### Customising shortcuts

Add a `"keybindings"` object to `~/.folio/web_config.json`. Only the keys you
want to change need to be specified — defaults are used for the rest.

```json
{
  "keybindings": {
    "go_library": "Alt+1",
    "go_setlists": "Alt+2",
    "toggle_fullscreen": "Ctrl+Shift+f"
  }
}
```

Binding format: modifiers joined with `+` before the key name.
Modifiers: `Ctrl`, `Alt`, `Shift`, `Meta`. Key names match
[KeyboardEvent.key](https://developer.mozilla.org/en-US/docs/Web/API/KeyboardEvent/key/Key_Values)
(e.g. `ArrowRight`, `Escape`, `a`, `F2`).

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
| `tests/test_web_core.py` | 67 | `web.core` module: path utils, SafeJSON, Score parsing, content hashing, library scanning (.exclude support), annotation load/save/migration, etag, conflict detection, tag renaming, PDF export with intrinsic-rotation handling |
| `tests/test_web_api.py` | 106 | FastAPI endpoints: config (keybindings), library, PDF serving, annotation CRUD, rotation, etag/conflict, setlist CRUD/rename, nested setlists (refs, flattening, cycle detection, rename cascading, backward compat), PDF export, content-hash reference healing, path traversal, security, auth |

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

The frontend is split into ES modules under `web/static/modules/`:

| Module | Description |
|---|---|
| `state.js` | Centralized application state |
| `api.js` | Fetch wrapper with retry, auth redirect, cache-busting |
| `dom.js` | DOM element references |
| `views.js` | View switching (library, setlists, recent, viewer) |
| `library.js` | Library loading, rendering, sorting, filtering |
| `viewer.js` | PDF rendering, page navigation, display modes, fullscreen |
| `annotations.js` | Drawing, tools, pointer events, save/load with etag concurrency |
| `setlists.js` | Setlist CRUD, drag-and-drop reorder, playback |
| `keyboard.js` | Configurable keyboard shortcuts (data-driven from server config) |
| `touch.js` | Touch gestures — swipe navigation, double-tap, scroll-boundary page turns |
| `recent.js` | Recent files list with content-hash based healing |
| `cache.js` | Offline cache UI (pin/unpin PDFs) |
| `dialog-handlers.js` | Per-dialog show/close logic |
| `theme.js` | Dark/light theme toggle |
| `utils.js` | Shared utilities (HTML escaping, coordinate transforms, constants) |

Other static files:

| File | Description |
|---|---|
| `app.js` | Entry point: imports, wiring, boot sequence |
| `app.css` | Dark/light theme, responsive layout, safe-area support |
| `index.html` | Single-page app shell |
| `sw.js` | Service worker: stale-while-revalidate PDF caching, offline support, LRU eviction |

### File naming convention

PDF filenames encode metadata: `Composer - Title -- tag1 tag2.pdf`

- `" - "` (space-dash-space) separates composer from title. Hyphenated composers (e.g. Rimsky-Korsakov) are safe.
- `" -- "` (space-double-dash-space) separates tags. Tags are space-delimited, lowercase. Use underscores for multi-word tags (e.g. `2nd_horn`).
- Subdirectory names become folder tags automatically (read-only).
- Files without `" - "` use the whole basename as the title, with composer set to "Unknown".

### Content-hash identity

Each PDF gets a fast content hash (SHA-256 of first/last 4 KB + file size). A persistent `_hash_index.json` in the library directory tracks hashes across scans. When a PDF is renamed or moved externally, the next library scan detects the change and automatically heals:
- Setlist references (updates paths in `setlists.json`)
- Annotation sidecars (moves the `.json` file to match the new PDF name)
- Recent list entries (client-side, matched by content hash)

### File formats

- **`setlists.json`** — setlist definitions, written to the root of the music library folder; see `docs/setlist-file-format.md` for the full specification.
- **`<score>.json`** — annotation sidecar written alongside each PDF; versioned JSON containing per-page annotation lists and rotation overrides.  Stored rotations are *additive on top of* the PDF's intrinsic `/Rotate` (i.e. relative to the canonical Acrobat orientation).
- **`_hash_index.json`** — content-hash-to-path index, auto-generated in the library directory; used to detect renames between scans.

### One-off scripts

- **`scripts/sort_tags.py`** — alphabetises tag lists in PDF filenames; updates hash index and setlists in lockstep.
- **`scripts/migrate_rotations.py`** — one-shot migration after the rotation-handling fix.  Subtracts each PDF's intrinsic `/Rotate` from the user-stored rotation in annotation sidecars, so previously-compensated pages stay visually identical after the fix.  Dry-run by default; `--apply` writes.  Only needs to be run once per library, and only matters if you'd previously rotated pages to compensate for upside-down PDFs.

### Keyboard shortcuts

See the [Keyboard Shortcuts](#keyboard-shortcuts) section above for defaults and customisation.
