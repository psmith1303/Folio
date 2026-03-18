"""
Core business logic extracted from MusicScoreViewer.py.

This module contains no Tkinter dependencies and is used by the web backend.
Error conditions raise exceptions instead of showing message dialogs.
"""

import json
import logging
import os
import re
import shutil
import sys
import tempfile
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
    tags: set[str] = field(default_factory=set)

    def __init__(self, filepath: str, filename: str,
                 folder_tags: set[str] | None = None) -> None:
        self.filepath = normalize_path(filepath)
        self.filename = filename
        self.composer = "Unknown"
        self.title = ""
        self.tags = set()
        if folder_tags:
            self.tags.update(t.lower() for t in folder_tags if t)
        self._parse()

    def _parse(self) -> None:
        try:
            base = os.path.splitext(self.filename)[0]
            if " -- " in base:
                parts = base.split(" -- ", 1)
                base = parts[0]
                self.tags.update(t.lower() for t in parts[1].split() if t)
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
        }


# ---------------------------------------------------------------------------
# Library scanning
# ---------------------------------------------------------------------------


def scan_library(path: str) -> list[Score]:
    """Walk *path* and return a Score for every PDF found."""
    path = normalize_path(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Directory not found: {path}")

    found: list[Score] = []
    for root_dir, _, files in os.walk(path):
        rel = os.path.normpath(os.path.relpath(root_dir, path))
        parts = rel.lower().replace("\\", "/").split("/")
        ftags = {p for p in parts if p and p != "."}
        for f in files:
            if f.lower().endswith(".pdf"):
                found.append(Score(os.path.join(root_dir, f), f, ftags))
    return found


# ---------------------------------------------------------------------------
# PDF metadata helper
# ---------------------------------------------------------------------------


def pdf_page_count(filepath: str) -> int:
    """Return the number of pages in a PDF without rendering anything."""
    import pymupdf as fitz

    doc = fitz.open(filepath)
    count = len(doc)
    doc.close()
    return count
