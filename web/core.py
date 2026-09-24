"""
Folio — core business logic.

Error conditions raise exceptions; the calling HTTP layer converts them
to proper API responses.
"""

import copy
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("folio")

# ---------------------------------------------------------------------------
# Path Utilities
# ---------------------------------------------------------------------------


def normalize_path(path: str) -> str:
    """Normalise a path to the OS-native separator.

    Translates between Windows drive-letter paths and WSL mount paths:
      Windows -> WSL:  Z:\\foo\\bar  ->  /mnt/z/foo/bar
      WSL -> Windows:  /mnt/z/foo/bar  ->  Z:\\foo\\bar
    """
    if not path:
        return path
    p = path.replace("\\", "/")
    if sys.platform != "win32":
        m = re.match(r'^([A-Za-z]):/(.*)', p)
        if m:
            p = f"/mnt/{m.group(1).lower()}/{m.group(2)}"
    else:
        m = re.match(r'^/mnt/([a-zA-Z])/(.*)', p)
        if m:
            p = f"{m.group(1).upper()}:/{m.group(2)}"
    return os.path.normpath(p)


def portable_path(path: str) -> str:
    """Convert a path to a portable storage form with forward slashes."""
    if not path:
        return path
    return path.replace("\\", "/")


# ---------------------------------------------------------------------------
# Content identity
# ---------------------------------------------------------------------------

_HASH_CHUNK = 4096


def compute_content_hash(filepath: str, size: int | None = None) -> str:
    """Compute a fast content hash from a file's first/last 4 KB and size.

    If *size* is supplied, the redundant stat is skipped — pass it when you
    already have a stat result for *filepath*.

    Returns a 12-char hex string, or "" if the file cannot be read.
    """
    try:
        if size is None:
            size = os.path.getsize(filepath)
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            head = f.read(_HASH_CHUNK)
            h.update(head)
            if size > _HASH_CHUNK * 2:
                f.seek(-_HASH_CHUNK, 2)
                h.update(f.read(_HASH_CHUNK))
            elif size > _HASH_CHUNK:
                h.update(f.read())
        h.update(str(size).encode())
        return h.hexdigest()[:12]
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# SafeJSON — Atomic JSON persistence
# ---------------------------------------------------------------------------


class SafeJSONError(Exception):
    """Raised when SafeJSON cannot load or save."""


_UNREAD = object()  # SafeJSON.save: the caller hasn't read the target


class SafeJSON:
    """Atomic JSON read/write.

    Errors raise SafeJSONError so the calling HTTP layer can return proper
    responses.
    """

    @staticmethod
    def read_bytes(filepath: str) -> bytes | None:
        """The file's bytes, or None if it doesn't exist.

        Raises SafeJSONError if it exists but can't be read.
        """
        try:
            with open(filepath, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None
        except OSError as e:
            log.error(f"Error reading JSON {filepath}: {e}")
            raise SafeJSONError(f"Error reading {filepath}: {e}") from e

    @staticmethod
    def parse(content: bytes, filepath: str):
        """Decode JSON *content* read from *filepath*.

        Raises SafeJSONError if it is corrupt.
        """
        try:
            return json.loads(content)
        except ValueError as e:
            log.error(f"Corrupt JSON in {filepath}: {e}")
            raise SafeJSONError(f"Corrupt JSON in {filepath}: {e}") from e

    @staticmethod
    def load(filepath: str, default=None):
        content = SafeJSON.read_bytes(filepath)
        if content is None:
            return default if default is not None else {}
        return SafeJSON.parse(content, filepath)

    @staticmethod
    def save(filepath: str, data, current: bytes | None | object = _UNREAD) -> bytes:
        """Write *data* as JSON atomically; return the bytes now in the file.

        The temp file is written beside the target, so os.replace is an
        atomic rename: a temp file elsewhere (e.g. /tmp, used after 7fdc80a
        saw hangs on an SMB drive) is on another filesystem from /mnt drives
        and Docker bind mounts, and copying it over the target is not
        atomic. The replacement keeps the target's permission bits (not
        its owner: the file is recreated by whoever saves). If the target already holds exactly these bytes it is left
        untouched (no mtime change, no file-sync churn); pass *current*
        (its bytes, or None if absent) when the caller has already read it.
        A target that exists but can't be read is overwritten. Raises
        SafeJSONError on failure.
        """
        content = json.dumps(data, indent=4).encode("utf-8")
        tmp_name = None
        try:
            dir_name = os.path.dirname(filepath)
            if dir_name and not os.path.exists(dir_name):
                raise SafeJSONError(
                    f"Cannot save — directory does not exist: {dir_name}"
                )
            if current is _UNREAD:
                try:
                    current = SafeJSON.read_bytes(filepath)
                except SafeJSONError:
                    current = None  # unreadable (logged): overwrite it
            if current == content:
                return content
            tmp_name = os.path.join(
                dir_name,
                f".{os.path.basename(filepath)}.{uuid.uuid4().hex[:8]}.tmp")
            with open(tmp_name, "xb") as f:
                f.write(content)
            try:
                shutil.copymode(filepath, tmp_name)
            except FileNotFoundError:
                pass  # a new file gets the default mode
            try:
                os.replace(tmp_name, filepath)
            except PermissionError:
                # Windows refuses to replace a file another process has
                # open; fall back to a (non-atomic) copy there only.
                if sys.platform != "win32":
                    raise
                log.warning(f"Non-atomic save of {filepath}: target in use")
                shutil.copyfile(tmp_name, filepath)
                os.remove(tmp_name)
            tmp_name = None
            return content
        except SafeJSONError:
            raise
        except Exception as e:
            raise SafeJSONError(f"Failed to save {filepath}: {e}") from e
        finally:
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Score data model
# ---------------------------------------------------------------------------


@dataclass
class Score:
    """A single PDF score parsed from a filename.

    Filename convention: ``Composer - Title -- tag1 tag2.pdf``
    """

    filepath: str
    filename: str
    composer: str = "Unknown"
    title: str = ""
    folder_tags: set[str] = field(default_factory=set)
    filename_tags: set[str] = field(default_factory=set)
    content_hash: str = ""
    mtime: float = 0.0

    @property
    def tags(self) -> set[str]:
        return self.folder_tags | self.filename_tags

    def __init__(self, filepath: str, filename: str,
                 folder_tags: set[str] | None = None) -> None:
        self.filepath = normalize_path(filepath)
        self.filename = filename
        self.composer = "Unknown"
        self.title = ""
        self.folder_tags = set()
        self.filename_tags = set()
        self.content_hash = ""
        self.mtime = 0.0
        if folder_tags:
            self.folder_tags.update(t.lower() for t in folder_tags if t)
        self._parse()

    def _parse(self) -> None:
        try:
            base = os.path.splitext(self.filename)[0]
            if " -- " in base:
                parts = base.split(" -- ", 1)
                base = parts[0]
                self.filename_tags.update(
                    t.lower() for t in parts[1].split() if t
                )
            if " - " in base:
                parts = base.split(" - ", 1)
                self.composer = parts[0].strip()
                self.title = parts[1].strip()
            else:
                self.title = base.strip()
        except Exception as exc:
            log.warning(f"Could not parse filename '{self.filename}': {exc}")

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict."""
        return {
            "filepath": portable_path(self.filepath),
            "filename": self.filename,
            "composer": self.composer,
            "title": self.title,
            "tags": sorted(self.tags),
            "folder_tags": sorted(self.folder_tags),
            "filename_tags": sorted(self.filename_tags),
            "content_hash": self.content_hash,
            "mtime": self.mtime,
        }


def build_tagged_filename(composer: str, title: str,
                          filename_tags: set[str],
                          ext: str = ".pdf") -> str:
    """Reconstruct a filename from its parsed components.

    Returns e.g. ``Bach - Suite -- jazz blues.pdf``.
    Tags are sorted for deterministic output.
    """
    if composer and composer != "Unknown":
        base = f"{composer} - {title}"
    else:
        base = title
    if filename_tags:
        tag_str = " ".join(sorted(filename_tags))
        base = f"{base} -- {tag_str}"
    return base + ext


def rename_score_tags(score: Score, new_tags: set[str]) -> Score:
    """Rename *score*'s file to carry *new_tags*; see rename_score_file.

    Returns *score* unchanged if its tags already match.
    """
    if new_tags == score.filename_tags:
        return score

    ext = os.path.splitext(score.filename)[1]
    new_filename = build_tagged_filename(
        score.composer, score.title, new_tags, ext
    )
    return rename_score_file(score, new_filename)


def rename_score_file(score: Score, new_filename: str) -> Score:
    """Rename *score*'s PDF within its directory, moving its sidecar with it.

    If the sidecar rename fails the PDF rename is rolled back and the error
    re-raised. Returns a new Score for the renamed file.
    Raises FileExistsError if the target filename is another existing file.
    A case-only rename is allowed: on case-insensitive filesystems (Windows,
    macOS, WSL /mnt drives) the "target" found is the source itself, and the
    directory lists only the old name. Any other existing target -- including
    a hard link to the source, even one whose name differs only in case -- is
    refused.
    """
    old_dir = os.path.dirname(score.filepath)
    new_filepath = os.path.join(old_dir, new_filename)

    case_only = (new_filepath != score.filepath
                 and new_filepath.lower() == score.filepath.lower())
    if os.path.exists(new_filepath) and not (
            case_only
            and os.path.samefile(score.filepath, new_filepath)
            and new_filename not in os.listdir(old_dir)):
        raise FileExistsError(f"Target file already exists: {new_filename}")

    # Rename PDF
    os.rename(score.filepath, new_filepath)

    # Rename sidecar JSON if it exists
    old_sidecar = annotation_sidecar_path(score.filepath)
    if os.path.exists(old_sidecar):
        new_sidecar = annotation_sidecar_path(new_filepath)
        try:
            os.rename(old_sidecar, new_sidecar)
        except OSError:
            # Roll back PDF rename
            os.rename(new_filepath, score.filepath)
            raise

    new_score = Score(new_filepath, new_filename, score.folder_tags)
    new_score.content_hash = score.content_hash
    return new_score


# ---------------------------------------------------------------------------
# Library scanning
# ---------------------------------------------------------------------------


def scan_library(
    path: str,
    hash_cache: dict | None = None,
) -> list[Score]:
    """Walk *path* and return a Score for every PDF found.

    Directories containing a ``.exclude`` file are skipped entirely.

    If *hash_cache* is provided, it is treated as a persistent map of
    ``{portable_path: {"size": int, "mtime": float, "hash": str}}``. Files
    whose size and mtime match the cached entry reuse the cached hash
    without re-reading the file. After scanning, *hash_cache* is mutated
    in place to reflect the current library (stale entries pruned, new
    entries added), so the caller can persist it.
    """
    path = normalize_path(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Directory not found: {path}")

    found: list[Score] = []
    new_cache: dict[str, dict] = {}

    def visit(dir_path: str) -> None:
        try:
            with os.scandir(dir_path) as it:
                entries = list(it)
        except OSError:
            return

        # Skip directories that contain a .exclude marker
        for e in entries:
            if e.name == ".exclude":
                try:
                    if e.is_file(follow_symlinks=False):
                        return
                except OSError:
                    return

        rel = os.path.normpath(os.path.relpath(dir_path, path))
        parts = rel.lower().replace("\\", "/").split("/")
        ftags = {p for p in parts if p and p != "."}

        subdirs: list[str] = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
                    continue
            except OSError:
                continue
            if not entry.name.lower().endswith(".pdf"):
                continue

            score = Score(entry.path, entry.name, ftags)

            try:
                st = entry.stat()
            except OSError:
                st = None

            if st is not None:
                score.mtime = st.st_mtime

            pkey = portable_path(entry.path)
            cached_hash = ""
            if hash_cache is not None and st is not None:
                prev = hash_cache.get(pkey)
                if (prev
                        and prev.get("size") == st.st_size
                        and prev.get("mtime") == st.st_mtime
                        and prev.get("hash")):
                    cached_hash = prev["hash"]

            if cached_hash:
                score.content_hash = cached_hash
            else:
                size = st.st_size if st is not None else None
                score.content_hash = compute_content_hash(entry.path, size=size)

            if hash_cache is not None and st is not None and score.content_hash:
                new_cache[pkey] = {
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "hash": score.content_hash,
                }

            found.append(score)

        for sd in subdirs:
            visit(sd)

    visit(path)

    if hash_cache is not None:
        hash_cache.clear()
        hash_cache.update(new_cache)

    return found


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

ANNOTATION_VERSION = 2


class AnnotationConflictError(Exception):
    """Raised when an annotation save conflicts with a concurrent edit."""


def annotation_sidecar_path(pdf_path: str) -> str:
    """Return the sidecar JSON path for a given PDF."""
    return os.path.splitext(normalize_path(pdf_path))[0] + ".json"


def _content_etag(content: bytes | None) -> str:
    """Etag for a sidecar's bytes; "" when there is no sidecar."""
    return "" if content is None else hashlib.sha256(content).hexdigest()[:16]


def _read_sidecar(sidecar: str) -> bytes | None:
    """The sidecar's bytes, or None if it doesn't exist or can't be read."""
    try:
        return SafeJSON.read_bytes(sidecar)
    except SafeJSONError:
        return None


def annotations_etag(pdf_path: str) -> str:
    """Compute an etag from the annotation sidecar file content.

    Returns an empty string if no sidecar exists.
    """
    return _content_etag(_read_sidecar(annotation_sidecar_path(pdf_path)))


def load_annotations(pdf_path: str) -> dict:
    """Load the annotation sidecar JSON for *pdf_path*.

    Returns a dict with keys: version, rotations, pages, etag.
    Migrates old formats and assigns missing UUIDs.
    """
    sidecar = annotation_sidecar_path(pdf_path)
    content = _read_sidecar(sidecar)
    etag = _content_etag(content)
    raw = {}
    if content is not None:
        try:
            raw = SafeJSON.parse(content, sidecar)
        except SafeJSONError:
            pass  # logged; treated as empty

    # Normalise structure
    if "version" not in raw:
        # Old format: top-level keys are page numbers → annotation lists
        pages = {}
        for k, v in raw.items():
            if isinstance(v, list):
                pages[k] = v
        raw = {"version": ANNOTATION_VERSION, "rotations": {}, "pages": pages}

    rotations = raw.get("rotations", {})
    pages = raw.get("pages", {})

    # Ensure every annotation has a UUID
    dirty = False
    for pg_annots in pages.values():
        for annot in pg_annots:
            if "uuid" not in annot:
                annot["uuid"] = str(uuid.uuid4())
                dirty = True

    if dirty:
        etag = save_annotations(pdf_path, pages, rotations)

    return {
        "version": ANNOTATION_VERSION,
        "rotations": rotations,
        "pages": pages,
        "etag": etag,
    }


def save_annotations(
    pdf_path: str,
    pages: dict,
    rotations: dict,
    expected_etag: str | None = None,
) -> str:
    """Save annotations and rotations to the sidecar JSON.

    If *expected_etag* is provided, the current file's etag must match or
    an ``AnnotationConflictError`` is raised.  Returns the new etag.
    """
    sidecar = annotation_sidecar_path(pdf_path)
    current = _read_sidecar(sidecar)
    if expected_etag is not None and _content_etag(current) != expected_etag:
        raise AnnotationConflictError(
            "Annotations were modified by another session"
        )

    # Only save non-zero rotations
    clean_rot = {k: v for k, v in rotations.items() if v % 360 != 0}
    data = {
        "version": ANNOTATION_VERSION,
        "rotations": clean_rot,
        "pages": pages,
    }
    return _content_etag(SafeJSON.save(sidecar, data, current))


# ---------------------------------------------------------------------------
# Library-relative paths
# ---------------------------------------------------------------------------
#
# Files that store score paths (setlists, the recent list, the hash index and
# the scan cache) hold them relative to the library root, so they stay valid
# wherever the library is mounted (host vs container, WSL vs Windows). In
# memory and over the API, paths stay absolute under the current root: the
# conversion happens only in the loaders and savers below.

# The files in a library root that store score paths.
SETLISTS_FILE = "setlists.json"
RECENT_FILE = "_recent.json"
HASH_INDEX_FILE = "_hash_index.json"
SCAN_CACHE_FILE = "_scan_cache.json"


def is_library_relative(path: str) -> bool:
    """True if stored *path* is in relative form. Anything starting with "/"
    counts as absolute on every OS (on Windows, Python 3.13+ ``isabs`` says
    otherwise for root-only paths such as ``/data/x.pdf``)."""
    p = portable_path(path)
    return not p.startswith("/") and not os.path.isabs(normalize_path(p))


def to_library_relative(path: str, root: str) -> str | None:
    """Return *path* relative to *root* in portable form, or None if it isn't
    inside *root* (or *root* is empty).

    Accepts relative paths and absolute ones in WSL (``/mnt/z/...``) or
    Windows (``Z:/...``) form; the two are treated as the same place.
    """
    if not root or not path:
        return None
    if is_library_relative(path):
        rel = posixpath.normpath(portable_path(path))
        return None if rel == ".." or rel.startswith("../") else rel
    abs_p = portable_path(normalize_path(portable_path(path)))
    r = portable_path(normalize_path(root)).rstrip("/")
    if abs_p.startswith(r + "/"):
        return abs_p[len(r) + 1:]
    return None


def from_library_relative(path: str, root: str) -> str:
    """Return the absolute portable path for a stored *path* under *root*.

    Relative paths are joined to *root*. Absolute paths (legacy entries) are
    normalised to *root*'s form when they lie inside it, else returned as-is.
    """
    rel = to_library_relative(path, root)
    if rel is None:
        return path
    return portable_path(normalize_path(root)).rstrip("/") + "/" + rel


def to_stored_path(path: str, root: str) -> str:
    """The form *path* is written to disk in: relative to *root* when it lies
    inside it, else unchanged (paths outside the library, or no library)."""
    rel = to_library_relative(path, root)
    return path if rel is None else rel


# Path walkers: apply *fn* to every stored path in a loaded file, in place,
# skipping malformed entries. Each returns the number of paths *fn* changed.

def map_setlist_paths(data: dict, fn: Callable[[str], str]) -> int:
    count = 0
    for sl in data.values():
        for item in sl["items"]:
            if not isinstance(item, dict) or item.get("type", "song") != "song":
                continue
            old = item.get("path")
            if isinstance(old, str) and (new := fn(old)) != old:
                item["path"] = new
                count += 1
    return count


def map_recent_paths(data: list, fn: Callable[[str], str]) -> int:
    count = 0
    for entry in data:
        if not isinstance(entry, dict):
            continue
        old = entry.get("filepath")
        if isinstance(old, str) and (new := fn(old)) != old:
            entry["filepath"] = new
            count += 1
    return count


def map_hash_index_paths(index: dict, fn: Callable[[str], str]) -> int:
    count = 0
    for content_hash, old in index.items():
        if isinstance(old, str) and (new := fn(old)) != old:
            index[content_hash] = new
            count += 1
    return count


def stored_paths(data, walk: Callable) -> list[str]:
    """Every path in loaded *data*, via its path walker."""
    found: list[str] = []
    walk(data, lambda p: found.append(p) or p)
    return found


def foreign_paths(data, walk: Callable, root: str) -> list[str]:
    """Paths in loaded *data* that don't lie inside *root*."""
    return [p for p in stored_paths(data, walk)
            if to_library_relative(p, root) is None]


# ---------------------------------------------------------------------------
# Setlists, the recent list and the hash index
# ---------------------------------------------------------------------------
#
# Each file has a raw reader (paths exactly as stored) and a path walker; the
# generic loader returns absolute paths under *root* and the generic saver
# writes relative ones, without modifying the caller's data. An empty *root*
# (no library set) stores paths unchanged.


def _normalize_setlist_value(v) -> dict:
    """Normalize a stored setlist value to {"items": [...], "shuffle": bool}.

    Older versions stored each setlist as a bare list of items; this lifts
    them into the new dict form transparently on load.
    """
    if isinstance(v, list):
        return {"items": v, "shuffle": False}
    if isinstance(v, dict):
        items = v.get("items")
        if not isinstance(items, list):
            items = []
        return {"items": items, "shuffle": bool(v.get("shuffle", False))}
    return {"items": [], "shuffle": False}


def _read_setlists(path: str) -> dict:
    raw = SafeJSON.load(path, default={})
    if not isinstance(raw, dict):
        return {}
    return {name: _normalize_setlist_value(v) for name, v in raw.items()}


def _read_recent(path: str) -> list:
    data = SafeJSON.load(path, default=[])
    return data if isinstance(data, list) else []


def _read_hash_index(path: str) -> dict:
    data = SafeJSON.load(path, default={})
    return data if isinstance(data, dict) else {}


# Files whose stored paths are migrated to relative form and healed after
# renames: file name -> (raw reader, path walker). The scan cache is not
# listed: its paths are keys rather than values, and it is a regenerable
# cache rewritten on every scan (see load_scan_cache/save_scan_cache).
PATH_STORES: dict[str, tuple[Callable[[str], object], Callable]] = {
    SETLISTS_FILE: (_read_setlists, map_setlist_paths),
    RECENT_FILE: (_read_recent, map_recent_paths),
    HASH_INDEX_FILE: (_read_hash_index, map_hash_index_paths),
}


def load_store(name: str, path: str, root: str):
    """Load the PATH_STORES file *name* at *path* with absolute paths under
    *root* (empty/default value if missing or of the wrong shape).

    Raises SafeJSONError if the file is unreadable or corrupt.
    """
    read, walk = PATH_STORES[name]
    data = read(path)
    walk(data, lambda p: from_library_relative(p, root))
    return data


def save_store(name: str, path: str, data, root: str) -> None:
    """Save the PATH_STORES file *name* with paths relative to *root*."""
    _read, walk = PATH_STORES[name]
    data = copy.deepcopy(data)
    walk(data, lambda p: to_stored_path(p, root))
    SafeJSON.save(path, data)


def load_setlists(path: str, root: str) -> dict:
    return load_store(SETLISTS_FILE, path, root)


def save_setlists(path: str, data: dict, root: str) -> None:
    save_store(SETLISTS_FILE, path, data, root)


def load_recent(path: str, root: str) -> list:
    return load_store(RECENT_FILE, path, root)


def save_recent(path: str, data: list, root: str) -> None:
    save_store(RECENT_FILE, path, data, root)


def load_hash_index(path: str, root: str) -> dict:
    return load_store(HASH_INDEX_FILE, path, root)


def save_hash_index(path: str, index: dict, root: str) -> None:
    save_store(HASH_INDEX_FILE, path, index, root)


def load_scan_cache(path: str, root: str) -> dict:
    """Load the scan cache (path -> {size, mtime, hash}) with absolute keys
    ({} if missing or not an object).

    Raises SafeJSONError if the file is unreadable or corrupt.
    """
    data = SafeJSON.load(path, default={})
    if not isinstance(data, dict):
        return {}
    return {from_library_relative(k, root): v for k, v in data.items()}


def save_scan_cache(path: str, cache: dict, root: str) -> None:
    SafeJSON.save(path, {to_stored_path(k, root): v for k, v in cache.items()})


def remap_setlist_paths(data: dict, remap: dict[str, str]) -> int:
    """Rewrite song paths in loaded setlists through *remap* (old -> new
    path), in place. Returns the number of items changed."""
    return map_setlist_paths(data, lambda p: remap.get(p, p))


def remap_recent_paths(data: list, remap: dict[str, str]) -> int:
    """Rewrite recent-list filepaths through *remap*, in place.
    Returns the number of entries changed."""
    return map_recent_paths(data, lambda p: remap.get(p, p))


def remap_hash_index(index: dict, remap: dict[str, str]) -> int:
    """Rewrite hash-index paths (content hash -> path) through *remap*, in
    place. Returns the number of entries changed."""
    return map_hash_index_paths(index, lambda p: remap.get(p, p))


# ---------------------------------------------------------------------------
# One-time migration to library-relative storage
# ---------------------------------------------------------------------------

MIGRATION_BACKUP_SUFFIX = ".pre-relative.bak"


def migrate_to_relative(library_dir: str) -> dict[str, tuple[int, list[str]]]:
    """Rewrite the PATH_STORES files in *library_dir* to relative form.

    A file that still holds absolute paths inside the library is first copied
    to ``<name>.pre-relative.bak`` (unless that backup already exists), then
    rewritten. Absolute paths outside the library are left as they are and
    reported. Returns {file name: (paths converted, paths left absolute)}.
    Raises SafeJSONError if a file is unreadable or corrupt, or can't be
    backed up or saved (e.g. a read-only library); it is safe to retry, as
    already-converted files are left alone.
    """
    report: dict[str, tuple[int, list[str]]] = {}
    for name, (read, walk) in PATH_STORES.items():
        file_path = os.path.join(library_dir, name)
        if not os.path.exists(file_path):
            continue
        stored = read(file_path)
        foreign = foreign_paths(stored, walk, library_dir)
        converted = sum(1 for p in stored_paths(stored, walk)
                        if p not in foreign and not is_library_relative(p))
        if converted:
            backup = file_path + MIGRATION_BACKUP_SUFFIX
            if not os.path.exists(backup):
                try:
                    shutil.copy2(file_path, backup)
                except OSError as e:
                    raise SafeJSONError(
                        f"Cannot back up {file_path} before converting it: {e}"
                    ) from e
            save_store(name, file_path, stored, library_dir)
        report[name] = (converted, foreign)
    return report
