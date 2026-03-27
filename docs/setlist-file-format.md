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
| `path` | string | **yes** | Portable path to the PDF file (see [Path encoding](#path-encoding)). |
| `title` | string | **yes** | Display title shown in the UI and window title bar. |
| `composer` | string | **yes** | Composer name (may be empty string `""`). |
| `start_page` | integer | **yes** | 1-based page number where playback of this item begins. Minimum value: `1`. |
| `end_page` | integer \| null | **yes** | 1-based page number where playback ends (inclusive). `null` means "last page of the PDF". |

#### Constraints

- `start_page` ≥ 1.
- `end_page` ≥ `start_page`, or `null`.
- If `start_page` exceeds the actual page count of the PDF at runtime, the viewer clamps it to `0` (first page, 0-based internally).
- If `end_page` exceeds the actual page count, the viewer clamps it to `total_pages - 1` (0-based internally).

#### Minimal valid song

```json
{
  "type": "song",
  "path": "Z:/Music/Scores/Bach/Goldberg.pdf",
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
  "path": "/mnt/z/Music/Scores/Bach/Goldberg.pdf",
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

Paths are stored in **portable form** (function `portable_path()`):

- Forward slashes only — no backslashes, so no escaping is needed in JSON.
- Windows absolute paths keep the drive letter: `Z:/PARA/Scores/foo.pdf`
- WSL/Linux mount paths are kept as-is: `/mnt/z/PARA/Scores/foo.pdf`

At read time, `normalize_path()` converts to the OS-native format and translates
between Windows (`Z:/...`) and WSL (`/mnt/z/...`) automatically, so a setlist
saved on Windows loads correctly on WSL and vice versa.

---

## Full example

```json
{
  "Warm-up": [
    {
      "type": "song",
      "path": "Z:/Music/Exercises/Long Tones.pdf",
      "title": "Long Tones",
      "composer": "",
      "start_page": 1,
      "end_page": null
    },
    {
      "type": "song",
      "path": "Z:/Music/Exercises/Scales.pdf",
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
      "path": "Z:/Music/Hymns/Amazing Grace.pdf",
      "title": "Amazing Grace",
      "composer": "Newton",
      "start_page": 1,
      "end_page": null
    }
  ],
  "Concert Programme": [
    {
      "type": "song",
      "path": "Z:/Music/Classical/Moonlight Sonata.pdf",
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

- Loaded on every API request via `SafeJSON.load(state.setlist_path())`.
- Written after every mutation (add setlist, rename, delete, reorder, add/remove
  item) via `SafeJSON.save(state.setlist_path(), data)`.
- `SafeJSON` writes atomically (temp file + `os.replace`) to avoid corruption on
  power loss.
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
