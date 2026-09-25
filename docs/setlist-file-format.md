# Setlist File Format

**File:** `setlists.json` (written to the root of the music library folder)

---

## Overview

The setlist file stores all user-defined setlists as a single JSON object.
Each key is a setlist name (string); each value is an ordered array of items.
Items can be **songs** (PDF references) or **setlist references** (pointers to
other setlists, enabling nested/reusable setlists).

---

## Top-level structure

```json
{
  "Setlist Name A": [ <item>, <item>, ... ],
  "Setlist Name B": [ <item>, ... ]
}
```

| Field | Type | Description |
|---|---|---|
| key | string | Setlist name, as entered by the user. Must be unique. |
| value | array | Ordered list of items — songs and/or setlist references (may be empty). |

---

## Item types

Each element of a setlist array is a JSON object with a `type` field that
determines its schema.  Legacy items without a `type` field are treated as
songs.

### Song item (`type: "song"`)

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | string | no | `"song"` (default if omitted for backward compatibility). |
| `path` | string | **yes** | Path to the PDF, relative to the library root (see [Path encoding](#path-encoding)). |
| `title` | string | **yes** | Display title shown in the UI and window title bar. |
| `composer` | string | **yes** | Composer name (may be empty string `""`). |
| `start_page` | integer | **yes** | 1-based page number where playback of this item begins. Minimum value: `1`. |
| `end_page` | integer \| null | **yes** | 1-based page number where playback ends (inclusive). `null` means "last page of the PDF". |

#### Constraints

- `start_page` ≥ 1.
- `start_page` of `1` means "not specified": if the PDF has a start-page stamp, the song starts on the stamped page instead (and going back from it moves to the previous song). Any other value is used as-is.
- `end_page` ≥ `start_page`, or `null`.
- If `start_page` exceeds the actual page count of the PDF at runtime, the viewer clamps it to `0` (first page, 0-based internally).
- If `end_page` exceeds the actual page count, the viewer clamps it to `total_pages - 1` (0-based internally).

#### Minimal valid song

```json
{
  "type": "song",
  "path": "Scores/Bach/Goldberg.pdf",
  "title": "Goldberg Variations",
  "composer": "Bach",
  "start_page": 1,
  "end_page": null
}
```

#### Song with page constraints

```json
{
  "type": "song",
  "path": "Scores/Bach/Goldberg.pdf",
  "title": "Aria",
  "composer": "Bach",
  "start_page": 3,
  "end_page": 4
}
```

### Setlist reference item (`type: "setlist_ref"`)

A setlist reference includes another setlist by name.  During playback the
reference is expanded recursively into a flat song list.

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | string | **yes** | Must be `"setlist_ref"`. |
| `setlist_name` | string | **yes** | Name of the referenced setlist. Must be non-empty. |

#### Example

```json
{
  "type": "setlist_ref",
  "setlist_name": "Warm-up"
}
```

#### Rules

- **Circular references** are rejected by the API at save time (400 error).
  This includes self-references and transitive cycles (A → B → A).
- **Diamond references** are allowed: if A includes B and C, and both B and C
  include D, then D's songs appear twice in the flattened list.
- **Dangling references** (pointing to a deleted setlist) are silently skipped
  during playback flattening and shown as "(missing)" in the UI.
- **Maximum nesting depth** is 10 levels (defence-in-depth).
- **Renaming** a setlist cascades to all references in other setlists.

---

## Path encoding

Paths are stored **relative to the library root**, with forward slashes only
(no escaping needed in JSON): `Scores/Bach/Goldberg.pdf`. The same rule applies
to `_recent.json`, `_hash_index.json` and `_scan_cache.json`. Because nothing
stored depends on where the library is mounted, the files stay valid on
Windows, WSL, inside the Docker container, or after the folder is moved.

In memory and over the HTTP API, paths are absolute under the current library
root; `load_setlists()` / `save_setlists()` in `web/core.py` convert at the
file boundary (`to_library_relative()` / `from_library_relative()`).

**Older files** stored absolute portable paths (`/mnt/z/psDATA/Scores/foo.pdf`
or Windows `Z:/psDATA/Scores/foo.pdf`, which are treated as the same place).
They are still read correctly, and are converted in place the first time the
library is opened; each converted file is first backed up as
`<name>.pre-relative.bak`. Absolute paths outside the library can't be
converted: they are left as they are and logged.

---

## Full example

```json
{
  "Warm-up": [
    {
      "type": "song",
      "path": "Exercises/Long Tones.pdf",
      "title": "Long Tones",
      "composer": "",
      "start_page": 1,
      "end_page": null
    },
    {
      "type": "song",
      "path": "Exercises/Scales.pdf",
      "title": "Scales",
      "composer": "",
      "start_page": 1,
      "end_page": 4
    }
  ],
  "Monday": [
    {
      "type": "setlist_ref",
      "setlist_name": "Warm-up"
    },
    {
      "type": "song",
      "path": "Hymns/Amazing Grace.pdf",
      "title": "Amazing Grace",
      "composer": "Newton",
      "start_page": 1,
      "end_page": null
    }
  ],
  "Concert Programme": [
    {
      "type": "song",
      "path": "Classical/Moonlight Sonata.pdf",
      "title": "Moonlight Sonata (1st mvt)",
      "composer": "Beethoven",
      "start_page": 1,
      "end_page": 6
    }
  ]
}
```

In this example, playing "Monday" first plays Long Tones and Scales (from the
"Warm-up" sub-setlist), then Amazing Grace.

---

## File location

The file is written to the **root of the currently-loaded music library folder**,
so setlists travel with the music collection (e.g. on a shared or portable drive).

The path is resolved by `AppState.setlist_path()` in `web/server.py`:

- When a library folder is loaded: `os.path.join(self.library_dir, "setlists.json")`.
- Fallback (no folder loaded yet): `os.path.join(CONFIG_DIR, "setlists.json")` — the
  `~/.folio/` directory.

Switching to a different library folder reloads setlists from that folder automatically.

---

## Persistence

- Loaded on every API request via `load_setlists()`, which returns absolute paths.
- Written after every mutation (add setlist, rename, delete, reorder, add/remove
  item) via `save_setlists()`, which stores paths relative to the library root.
- `SafeJSON` writes atomically (a temp file beside the target, then
  `os.replace`) to avoid corruption on power loss, and skips the write when the
  content is unchanged.
- If the file is absent, `SafeJSON.load` returns `{}` (no setlists).
- If the file is corrupt JSON, `{}` is returned.

---

## Editing by hand

For Emacs users, `setlist-editor.el` (project root) provides a friendlier
alternative: `M-x setlist-edit` opens the file as aligned org-mode tables
where page numbers can be changed with standard table navigation, and
`C-c C-s` writes back to JSON.  Setlist references appear as `>>Name` in the
Title column.  No raw JSON editing required.

For other editors, the file is plain JSON and can be edited directly.
Guidelines:

- Ensure the top-level structure is a JSON **object** (curly braces), not an array.
- Each setlist value must be a JSON **array** (square brackets), even if empty (`[]`).
- Each item should have a `type` field: `"song"` or `"setlist_ref"`.  Items
  without `type` are treated as songs for backward compatibility.
- `end_page` must be a JSON integer **or** the JSON literal `null` — not an empty
  string.
- Paths should use forward slashes.  Backslashes will be normalised at read time
  but are harder to read.
- Do not create circular references (A includes B, B includes A) — the API
  will reject them.
