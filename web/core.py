"""
Folio — core business logic.

Error conditions raise exceptions; the calling HTTP layer converts them
to proper API responses.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field

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
# SafeJSON — Atomic JSON persistence (no Tk dialogs)
# ---------------------------------------------------------------------------


class SafeJSONError(Exception):
    """Raised when SafeJSON cannot load or save."""


class SafeJSON:
    """Atomic JSON read/write.

    Unlike the Tk version, errors raise SafeJSONError instead of showing
    message dialogs, so the calling HTTP layer can return proper responses.
    """

    @staticmethod
    def load(filepath: str, default=None):
        if not os.path.exists(filepath):
            return default if default is not None else {}
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logging.error(f"Corrupt JSON in {filepath}: {e}")
            raise SafeJSONError(f"Corrupt JSON in {filepath}: {e}") from e
        except Exception as e:
            logging.error(f"Error reading JSON {filepath}: {e}")
            raise SafeJSONError(f"Error reading {filepath}: {e}") from e

    @staticmethod
    def save(filepath: str, data) -> None:
        """Write data via a local temp file then move/copy to the destination.

        Raises SafeJSONError on failure.
        """
        tmp_name = None
        try:
            dir_name = os.path.dirname(filepath)
            if dir_name and not os.path.exists(dir_name):
                raise SafeJSONError(
                    f"Cannot save — directory does not exist: {dir_name}"
                )
            fd, tmp_name = tempfile.mkstemp(text=True)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
            try:
                os.replace(tmp_name, filepath)
            except OSError:
                shutil.copyfile(tmp_name, filepath)
                os.remove(tmp_name)
            tmp_name = None
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
            logging.warning(f"Could not parse filename '{self.filename}': {exc}")

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
    """Rename a score's file on disk to reflect *new_tags*.

    Also renames the annotation sidecar JSON if it exists.
    Returns a new Score with updated filepath/filename/tags.
    Raises FileExistsError if the target filename already exists.
    """
    if new_tags == score.filename_tags:
        return score

    ext = os.path.splitext(score.filename)[1]
    new_filename = build_tagged_filename(
        score.composer, score.title, new_tags, ext
    )
    old_dir = os.path.dirname(score.filepath)
    new_filepath = os.path.join(old_dir, new_filename)

    if os.path.exists(new_filepath):
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
# PDF metadata helper
# ---------------------------------------------------------------------------


def pdf_page_count(filepath: str) -> int:
    """Return the number of pages in a PDF without rendering anything."""
    import pymupdf as fitz

    with fitz.open(filepath) as doc:
        return len(doc)


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

ANNOTATION_VERSION = 2


class AnnotationConflictError(Exception):
    """Raised when an annotation save conflicts with a concurrent edit."""


def annotation_sidecar_path(pdf_path: str) -> str:
    """Return the sidecar JSON path for a given PDF."""
    return os.path.splitext(normalize_path(pdf_path))[0] + ".json"


def annotations_etag(pdf_path: str) -> str:
    """Compute an etag from the annotation sidecar file content.

    Returns an empty string if no sidecar exists.
    """
    sidecar = annotation_sidecar_path(pdf_path)
    if not os.path.exists(sidecar):
        return ""
    try:
        with open(sidecar, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def load_annotations(pdf_path: str) -> dict:
    """Load the annotation sidecar JSON for *pdf_path*.

    Returns a dict with keys: version, rotations, pages, etag.
    Migrates old formats and assigns missing UUIDs.
    """
    sidecar = annotation_sidecar_path(pdf_path)
    try:
        raw = SafeJSON.load(sidecar, default={})
    except SafeJSONError:
        raw = {}

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
        save_annotations(pdf_path, pages, rotations)

    return {
        "version": ANNOTATION_VERSION,
        "rotations": rotations,
        "pages": pages,
        "etag": annotations_etag(pdf_path),
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
    if expected_etag is not None:
        current = annotations_etag(pdf_path)
        if current != expected_etag:
            raise AnnotationConflictError(
                "Annotations were modified by another session"
            )

    sidecar = annotation_sidecar_path(pdf_path)
    # Only save non-zero rotations
    clean_rot = {k: v for k, v in rotations.items() if v % 360 != 0}
    data = {
        "version": ANNOTATION_VERSION,
        "rotations": clean_rot,
        "pages": pages,
    }
    SafeJSON.save(sidecar, data)
    return annotations_etag(pdf_path)


# ---------------------------------------------------------------------------
# Annotation export (bake into PDF)
# ---------------------------------------------------------------------------

_CSS_TO_RGB: dict[str, tuple[float, float, float]] = {
    "black": (0, 0, 0),
    "red": (1, 0, 0),
    "blue": (0, 0, 1),
    "green": (0, 0.502, 0),
    "orange": (1, 0.647, 0),
    "purple": (0.502, 0, 0.502),
    "magenta": (1, 0, 1),
}

# Glyphs that render tiny in fall-back fonts and need ~6x scaling to be
# readable as music notation. Dynamics ("p", "f", ...) are intentionally
# excluded \u2014 they're plain letters that follow the size slider.
_NOTE_GLYPHS = {
    "\U0001D15E", "\u2669", "\u2669.", "\u266A",
    "\u266D", "\u266F", "\u266E",
}


def export_annotated_pdf(pdf_path: str) -> bytes:
    """Render annotations onto a copy of the PDF and return the bytes."""
    import pymupdf as fitz

    data = load_annotations(pdf_path)
    pages = data.get("pages", {})
    rots = data.get("rotations", {})

    with fitz.open(pdf_path) as doc:
        for pg_str, annots in pages.items():
            pg_num = int(pg_str)
            if pg_num >= len(doc):
                continue
            page = doc[pg_num]
            w = page.rect.width
            h = page.rect.height

            for a in annots:
                if a.get("type") == "ink":
                    _export_ink(page, a, w, h)
                elif a.get("type") == "text":
                    _export_text(page, a, w, h)

            rot = rots.get(pg_str, 0) % 360
            if rot:
                page.set_rotation((page.rotation + rot) % 360)

        return doc.tobytes()


def _export_ink(page, annot: dict, w: float, h: float) -> None:
    """Draw an ink stroke onto *page*.

    Annotation coords are normalized in the page's canonical (post-rotation)
    space, but PyMuPDF's draw primitives consume mediabox (pre-rotation)
    coords. Map each point through ``page.derotation_matrix`` so strokes land
    correctly on PDFs whose intrinsic ``/Rotate`` is non-zero.
    """
    import pymupdf as fitz

    pts = annot.get("points", [])
    if len(pts) < 2:
        return
    color = _CSS_TO_RGB.get(annot.get("color", "black"), (0, 0, 0))
    width = max(0.5, annot.get("width", 2) * 0.75)
    derot = page.derotation_matrix
    points = [fitz.Point(p[0] * w, p[1] * h) * derot for p in pts]
    shape = page.new_shape()
    shape.draw_polyline(points)
    shape.finish(color=color, width=width, lineCap=1, lineJoin=1)
    shape.commit()


def _export_text(page, annot: dict, w: float, h: float) -> None:
    """Insert a text annotation onto *page*.

    Centering is applied in canonical space, then the anchor is mapped to
    mediabox coords via ``page.derotation_matrix``. ``rotate=page.rotation``
    pre-rotates the glyphs so they read horizontally in the canonical view
    on PDFs with intrinsic ``/Rotate`` non-zero.
    """
    import pymupdf as fitz

    text = annot.get("text", "")
    if not text:
        return
    x = annot.get("x", 0) * w
    y = annot.get("y", 0) * h
    color = _CSS_TO_RGB.get(annot.get("color", "black"), (0, 0, 0))
    size = 12 + (annot.get("size", 2)) * 4
    if text in _NOTE_GLYPHS:
        size = round(size * 6)
    fontname = "helv"
    lines = text.split("\n")
    line_h = size * 1.2
    start_y = y - ((len(lines) - 1) * line_h) / 2
    for i, line in enumerate(lines):
        if not line:
            continue
        text_w = fitz.get_text_length(line, fontname=fontname, fontsize=size)
        baseline_y = start_y + i * line_h + size * 0.35
        anchor = fitz.Point(x - text_w / 2, baseline_y) * page.derotation_matrix
        page.insert_text(
            anchor,
            line,
            fontname=fontname,
            fontsize=size,
            color=color,
            rotate=page.rotation % 360,
        )
