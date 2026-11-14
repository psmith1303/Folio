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
- Offline use (PWA): each device keeps every setlist's PDFs pinned automatically (released when no setlist holds them), keeps the Recent list's PDFs (the last 200 opened) cached, and auto-caches up to 500 more viewed PDFs, least recently used evicted first. It syncs at launch and after a setlist change on that device, and asks the browser for persistent storage
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
| `tests/test_web_core.py` | 61 | `web.core` module: path utils, SafeJSON, Score parsing, content hashing, library scanning (.exclude support), annotation load/save/migration, etag, conflict detection, tag renaming |
| `tests/test_lighten_scans.py` | 31 | `scripts/lighten_scans.py`: A4 fit, Otsu, speck and edge-strip removal, deskew, `.exclude` folders, options, done-list; a quarter-turned page rendered at its displayed size, and a wrong-shaped render refused (pdftoppm faked); two end-to-end runs, one with pages turned by `/Rotate` 90 and 270 (needs poppler and jbig2, skips without them, as in the deploy image) |
| `tests/test_music_refresh.py` | 24 | `scripts/music_refresh.py` (lightening stubbed): every class (new, adopt only when every byte matches, conflict, changed, a new timestamp only, moved, renamed in Folio, deleted, deleted from both, gone), prune only with an identical Master copy (a .exclude marker kept while it hides a kept file), only folders the run emptied removed, no rename or move over another file, odd sidecar json, dry run writes nothing, restart after a crash, the swapped-folders guard, lighten options and jbig2 needed only for `--apply` in bw mode |
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
| `cache.js` | Offline cache UI (pin/unpin PDFs) and the sync that keeps setlist and Recent PDFs offline |
| `offline-lru.js` | Offline PDF cache bookkeeping shared with the service worker (cache name and keys, LRU store with manual pins and setlist holds, eviction); the only code that writes the PDF cache or the store, and eviction first reconciles the two; a classic script, loaded by `sw.js` via `importScripts` and by `cache.js` via import |
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

### Deploying

`git push p3800 master` deploys to the Docker host (p3800). Its hook builds the image's `test` stage from the pushed commit (a failure rejects the push), rebuilds and recreates only the `folio` container from it, then checks that it serves the new version as uid 1000 with a non-empty library, also at the public URL (bin's `deploy-stack`). `git push origin master` updates GitHub only.

The version is set in one place, `APP_VERSION` in `web/static/sw.js`; the server reads it from there at startup (and fails to start without it). Bump it for every front-end change, so installed copies replace their cached shell.

### Music-Master and the light library

Heavy scans (600 dpi, 30–120 MP a page) don't open on an iPhone. So the
library Folio serves (`psDATA/Resources/Music`) holds light copies, and the
originals live in `psDATA/Resources/Music-Master`:

- **Music-Master**: the original scans and everything kept with them
  (audio, MIDI, LilyPond, `.exclude` folders). No json. Add new scores
  **here**.
- **Music**: what Folio shows, and nothing else: a copy of each PDF outside
  a `.exclude` folder, plus Folio's json (sidecars, `setlists.json`,
  `_hash_index.json`, ...). A PDF over 16.7 MP on a page, or with pages
  more than 10% bigger than A4, is lightened by `scripts/lighten_scans.py`:
  pages shrunk to A4, straightened, black-and-white JBIG2 at 300 dpi, with
  specks and edge shadows removed. Other PDFs are copied as they are.

`scripts/music_refresh.py` brings Music up to date with Music-Master. Run it
on okapi; a dry run comes first and writes nothing:

```
python3 scripts/music_refresh.py            # what would change
python3 scripts/music_refresh.py --apply    # do it (restartable)
```

- New and changed Master PDFs are lightened or copied. A copy replaces its
  PDF at the same path, so its sidecar, setlist entries and Recent entry
  still match. A Master file whose timestamp moved but whose content didn't
  (a sync can do that) is not redone.
- A PDF moved or renamed in Master: its Music copy and sidecar move too.
  A PDF renamed in Folio (tag edits rename files): the Master file is
  renamed to match. That rename is the refresh's only write to Master.
- A PDF deleted from Master is reported; `--prune` deletes its Music copy
  (never the sidecar). A copy deleted from Music isn't made again unless
  `--restore` is given.
- Other files in Music (audio, `.exclude` folders) are deleted only when
  Master holds a byte-identical copy; anything else is reported as
  MUSIC-ONLY and kept, and a `.exclude` marker stays while it still hides
  a kept file. A Music file that differs from Master at the same path is a
  CONFLICT, left alone, as is a move or rename onto a path that already
  holds another file.
- `ANNOTATED` lists lightened PDFs that have annotations: straightening
  moves the music slightly, so check their marks.
- State is in `Music/.music-refresh.tsv`.

After a refresh that replaced PDFs, the iPhone and iPad still hold the old
copies offline, and show them once before revalidating: clear the offline
cache and re-pin setlists.

`scripts/lighten_scans.py` also runs on its own, writing copies to
`--out` (default `C:\Temp\Output`): `--dry-run` lists what it would do,
and a done-list lets an interrupted run restart. `--help` and its
docstring list the options.

The one-off move to this layout (2026-11):

```
rsync -a --exclude='*.json' /mnt/z/psDATA/Resources/Music/ /mnt/z/psDATA/Resources/Music-Master/
python3 scripts/music_refresh.py
python3 scripts/music_refresh.py --apply
```

The first run finds every PDF already in Music (ADOPT), lightens the heavy
ones in place, and deletes from Music the files Folio doesn't show (PRUNE).

### One-off scripts

- **`scripts/music_refresh.py`**, **`scripts/lighten_scans.py`** — fill the library from Music-Master with light copies (above).
- **`scripts/sort_tags.py`** — puts PDF filename tags into canonical form (lowercase, de-duplicated, sorted); updates the hash index, setlists and recent list in lockstep.
- **`scripts/migrate_rotations.py`** — one-shot migration after the rotation-handling fix.  Subtracts each PDF's intrinsic `/Rotate` from the user-stored rotation in annotation sidecars, so previously-compensated pages stay visually identical after the fix.  Dry-run by default; `--apply` writes.  Only needs to be run once per library, and only matters if you'd previously rotated pages to compensate for upside-down PDFs.

### Keyboard shortcuts

See the [Keyboard Shortcuts](#keyboard-shortcuts) section above for defaults and customisation.
