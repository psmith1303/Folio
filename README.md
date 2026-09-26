# Folio

A web application to view, navigate, and annotate PDF music scores.
Runs on any device with a browser, including iPad.

## Features
- PDF viewing with three display modes: Fit (single page), Wide (full width, scroll vertically), and 2-up (side-by-side)
- Annotations: pen (freehand ink), text, eraser, per-page undo, Clear Page (one-click erase of all annotations on the current page, with confirmation and undo)
- 7-colour palette, adjustable text and stamp size, musical symbol shortcuts
- Pen style grid: pick width (Fine to Heavy) and transparency (Solid, Semi, Highlight) with one tap from sample strokes; widths scale with the page
- Touch and Apple Pencil support (Pointer Events API), with optional Pencil-only mode that ignores finger/mouse input on the pen tool for palm-rejection during writing
- Metadata search by composer, title, and folder tags
- Start-page stamp: mark the page a score should open on; Library, Recent, Newest and setlists (whose start page is left at 1) open there
- Add scores to setlists directly from the viewer (`s` key)
- Click-to-navigate in Fit/2-up modes: right/bottom half = next page, left/top half = previous
- Wide mode: scroll vertically, arrow/space keys scroll natively; page turns via toolbar, PageUp/PageDown, or scroll-boundary in fullscreen
- Configurable keyboard shortcuts for navigation, tools, and view switching
- Content-hash based identity: renamed/moved PDFs are auto-detected on rescan, healing setlist references, annotation sidecars, and recent list entries
- Setlist management: create, edit, reorder, rename, delete, playback with page constraints
- Nested setlists: setlists can reference other setlists as sub-items, with automatic flattening for playback
- Dark/light theme toggle (remembered across sessions)
- Fullscreen mode for distraction-free viewing (f key or toolbar button)
- Directories with a `.exclude` file are hidden from the library

## Requirements
- Python 3.10+
- Dependencies: `fastapi`, `uvicorn`

## How to Run

### WSL / Debian / Ubuntu (recommended)
```
sudo apt install python3-uvicorn python3-fastapi
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

### Docker

A `Dockerfile` is included; run it like any other container, with two volumes:

- **Music library** — bind-mount it read-write, e.g. at `/library`. Stored
  setlists, the recent list and the hash index hold paths relative to the
  library root, the API gives and takes paths in the same form, and
  annotation sidecars sit next to each PDF — so neither the stored data nor
  each device's offline copies depend on the mount path.
- **Config** — the app keeps its settings in `~/.folio/web_config.json`. The
  image sets `HOME=/config`, so persist `/config` (e.g. `./folio-config:/config`)
  to keep your library selection across container recreations.

> **Gotcha — no scores listed?** A fresh `web_config.json` has no library
> selected, so the library is empty until you pick the folder once (via the
> first-launch dialog, or `POST /api/library {"path": "..."}`). The bind mount
> being present is not enough — Folio only scans the folder you point it at.
> Once set, `last_directory` is saved to the persisted config and auto-loaded on
> every restart.

## Security

Folio has **no authentication**. Anyone who can reach the port can read and
modify your library. Run it only on a network you trust, and do not expose it
to the internet.

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
| `tests/test_web_core.py` | 59 | `web.core` module: path utils, SafeJSON, Score parsing, content hashing, library scanning (.exclude support), annotation load/save/migration, etag, conflict detection, tag renaming |
| `tests/test_web_api.py` | 134 | FastAPI endpoints: config (keybindings), library, PDF serving, annotation CRUD, rotation, etag/conflict, setlist CRUD/rename, nested setlists (refs, flattening, cycle detection, rename cascading, backward compat), content-hash reference healing, path traversal, security |

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
| `api.js` | Fetch wrapper with retry, cache-busting |
| `dom.js` | DOM element references |
| `views.js` | View switching; `navigate(view)` for the nav bar and its shortcuts (leave the viewer, show the list view, reload it) |
| `library.js` | Library loading and rendering |
| `library-filter.js` | Pure filtering, sorting and search over the loaded library (library view and setlist song picker) |
| `score-table.js` | Shared row rendering for the Library, Recent and Newest tables: one click listener per table (open, or toggle the cache button) |
| `viewer.js` | PDF rendering, page navigation, display modes, fullscreen |
| `annotations.js` | Drawing, tools, pointer events, save/load with etag concurrency |
| `pen-style.js` | Pen style grid (width × transparency): the toolbar chip, the grid dialog and the saved choice |
| `annot-outbox.js` | Annotations saved while offline: kept per score in IndexedDB, shown on reopening, synced with a three-way merge (both sides' additions kept, deletions win) when back online |
| `setlists.js` | Setlist CRUD, drag-and-drop reorder, playback |
| `keyboard.js` | Configurable keyboard shortcuts (data-driven from server config) |
| `touch.js` | Touch gestures — swipe navigation, double-tap, scroll-boundary page turns |
| `recent.js` | Recent files list with content-hash based healing |
| `cache.js` | Offline cache UI (pin/unpin PDFs) |
| `offline-lru.js` | Offline PDF cache bookkeeping shared with the service worker (cache name and keys, LRU store, eviction); a classic script, loaded by `sw.js` via `importScripts` and by `cache.js` via import |
| `dialog-handlers.js` | Per-dialog show/close logic |
| `theme.js` | Dark/light theme toggle |
| `utils.js` | Shared utilities (HTML escaping, coordinate transforms, page scale, saved preferences, constants) |

Other static files:

| File | Description |
|---|---|
| `app.js` | Entry point: imports, wiring, boot sequence |
| `app.css` | Dark/light theme, responsive layout, safe-area support |
| `index.html` | Single-page app shell |
| `sw.js` | Service worker: stale-while-revalidate PDF caching (conditional: 304 when unchanged), offline support, LRU eviction |

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

- **`scripts/deploy.sh`** — deploys to the Docker host (p3800): checks the build inputs are committed and the versions agree, waits for the host's Syncthing copy of them to match this one byte for byte, rebuilds and recreates only the `folio` container, then verifies the served version, uid 1000, the library and the public URL. `--check` changes nothing; `--build-only` leaves the container alone.
- **`scripts/sort_tags.py`** — puts PDF filename tags into canonical form (lowercase, de-duplicated, sorted); updates the hash index, setlists and recent list in lockstep.
- **`scripts/migrate_rotations.py`** — one-shot migration after the rotation-handling fix.  Subtracts each PDF's intrinsic `/Rotate` from the user-stored rotation in annotation sidecars, so previously-compensated pages stay visually identical after the fix.  Dry-run by default; `--apply` writes.  Only needs to be run once per library, and only matters if you'd previously rotated pages to compensate for upside-down PDFs.

### Keyboard shortcuts

See the [Keyboard Shortcuts](#keyboard-shortcuts) section above for defaults and customisation.
