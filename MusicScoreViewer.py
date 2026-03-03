#!/usr/bin/env python3
"""
Music Score Viewer
==================
Version: 1.7.4

A robust Python application to view and annotate PDF music scores.

Changes in v1.7.4:
22. IMPROVEMENT: setlists.json is now stored in the root of the music library
    folder rather than the app directory, so setlists travel with the music
    collection (e.g. on a shared or portable drive). Switching to a different
    folder automatically reloads the setlists for that folder.

Changes in v1.7.3:
19. FIX: SafeJSON.save now returns bool and shows a visible error dialog on
    failure (directory missing or write error) instead of silently discarding
    data.  Previously, a missing target directory would log a warning and
    return with no user notification, causing silent data loss.
20. IMPROVEMENT: Dependency versions pinned in requirements.txt
    (pymupdf>=1.25,<2.0; Pillow>=11.0,<12.0) for reproducible installs.
21. FIX: README corrected (wrong script filename) and updated to reflect
    current features.

Changes in v1.7.2:
16. IMPROVEMENT: Added portable_path() helper — paths are now stored in JSON
    using forward slashes (Z:/PARA/...) so the file is human-readable without
    backslash escaping issues.  normalize_path() still converts to OS-native
    separators at the point of filesystem access.
17. FIX: When a setlist item's file cannot be found, a dialog now offers
    Skip (advance to next song), Locate (browse for the file and update the
    stored path permanently), or Cancel (exit setlist mode).
18. FIX: SafeJSON.load now shows a visible warning dialog when a JSON file
    cannot be parsed, instead of silently returning empty data.

Changes in v1.7.1:
15. FIX: normalize_path now translates between Windows drive-letter paths
    (Z:/...) and WSL mount paths (/mnt/z/...) so that setlists saved on one
    platform load correctly on the other.

Changes in v1.7.0:
14. FEATURE: Per-page rotation tool. Two toolbar buttons (↻ / ↺) rotate the
    current page 90° clockwise or counter-clockwise. Rotation is stored in the
    annotation sidecar JSON (non-destructive; the PDF file is never modified).
    Existing ink and text annotations are coordinate-transformed automatically
    when rotation changes, so they remain correctly placed. Keybindings [ and ]
    rotate left/right. The rotation indicator label shows the current angle.

Changes in v1.6.0 (bug-fix release):
1. FIX: PDF file handle leak in _close_score — doc.close() is now called before
   clearing self.doc, preventing OS file descriptor leaks (especially on Windows).
2. FIX: UI thread freeze during directory scan — _load_dir now runs os.walk in a
   background thread, keeping the UI responsive on large libraries.
3. FIX: Text search now includes composer names as well as titles, matching the
   behaviour of the score-picker dialog.
4. FIX: ignore_events flag can no longer get stuck True if _apply_filters raises —
   the reset is now inside a try/finally block.
5. FIX: Old-format annotations (no version key) are now immediately re-saved in the
   current format on load, preventing duplicate UUID assignment on every open.
6. FIX: _maximize now catches only tk.TclError instead of a bare except, so
   unrelated startup errors are no longer silently swallowed.
7. FIX: _quick_add_to_setlist now guards against current_score_path being None,
   preventing a TypeError crash when called with no score open.
8. FIX: _on_resize is now debounced — expensive PDF re-rasterisation is deferred
   150 ms after the last resize event instead of firing on every pixel.
9. FIX: _set_color no longer implicitly switches the active tool — picking a colour
   only sets the colour; it does not activate the pen.
10. FIX: Dead variable btn_f in TextEntryDialog removed.
11. FIX: Bare except clauses in ScorePickerDialog and QuickAddSetlistDialog now
    catch ValueError specifically and log the issue.
12. IMPROVEMENT: Added active-tool highlighting to toolbar buttons so the current
    tool is always visually clear.
13. IMPROVEMENT: Undo (Ctrl+Z) support for annotations — the last annotation action
    can be undone, per page.

Usage:
    python MusicScoreViewer.py [options]
"""

import sys
import os
import json
import copy
import logging
import argparse
import uuid
import tempfile
import shutil
import time
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, simpledialog
from collections import deque
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Path Determination & Cross-Platform Setup
# ---------------------------------------------------------------------------

def get_writable_app_dir() -> str:
    """
    Determines the best location for config files.
    Priority 1: Same directory as executable (Portable Mode).
    Priority 2: User Home directory (if app dir is read-only).
    Priority 3: System temp directory (last resort).
    """
    if getattr(sys, 'frozen', False):
        app_path = os.path.dirname(sys.executable)
    else:
        app_path = os.path.dirname(os.path.abspath(__file__))

    try:
        test_file = os.path.join(app_path, ".test_write_perm")
        with open(test_file, 'w') as f:
            f.write("test")
        os.remove(test_file)
        return app_path
    except PermissionError:
        user_dir = os.path.join(os.path.expanduser("~"), ".music_score_viewer")
        if not os.path.exists(user_dir):
            try:
                os.makedirs(user_dir)
            except OSError:
                return tempfile.gettempdir()
        return user_dir


APP_DIR = get_writable_app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SETLIST_PATH = os.path.join(APP_DIR, "setlists.json")

# ---------------------------------------------------------------------------
# Default Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "version": "1.7.4",
    "ui": {
        "window_size": "1200x900",
        "bg_color": "#333333",
        "toolbar_color": "#e0e0e0",
        "default_pen_color": "black",
        "default_pen_size": 2,
    },
    "behavior": {
        "last_directory": "",
        "annotation_version": 2,
    },
    "keybindings": {
        "next_page":       ["<space>", "n", "<Right>", "<Down>", "<Next>"],
        "prev_page":       ["<BackSpace>", "p", "<Left>", "<Up>", "<Prior>"],
        "first_page":      ["<Home>"],
        "last_page":       ["<End>"],
        "close_score":     ["<Escape>"],
        "search_focus":    ["<Control-f>"],
        "filter_composer": ["<Alt-c>"],
        "reset_filters":   ["<Alt-r>"],
        "go_to_page":      ["<Alt-p>", "<Alt-P>"],
        "undo":            ["<Control-z>"],
        "rotate_cw":       ["]"],
        "rotate_ccw":      ["["],
    },
}

APP_VERSION      = DEFAULT_CONFIG["version"]
DEFAULT_WIN_SIZE = DEFAULT_CONFIG["ui"]["window_size"]
BG_COLOR         = DEFAULT_CONFIG["ui"]["bg_color"]
TOOLBAR_COLOR    = DEFAULT_CONFIG["ui"]["toolbar_color"]
ANNOTATION_VERSION = DEFAULT_CONFIG["behavior"]["annotation_version"]

# Typeahead reset interval for the composer combobox (seconds)
COMBO_TYPEAHEAD_RESET_SECS = 1.0

# Resize debounce delay (milliseconds)
RESIZE_DEBOUNCE_MS = 150

MUSICAL_SYMBOLS_SET = {
    "\U0001D15E", "♩", "♩.", "♪",
    "pp", "p", "mp", "mf", "f", "ff",
    "sfz", "cresc", "dim",
}

# ---------------------------------------------------------------------------
# Path Utilities
# ---------------------------------------------------------------------------

def normalize_path(path: str) -> str:
    """
    Normalise a path to the OS-native separator and resolve any redundant
    components.  Also translates between Windows drive-letter paths and WSL
    mount paths so that a setlist saved on one platform loads correctly on
    the other:
      Windows -> WSL:  Z:\\foo\\bar  ->  /mnt/z/foo/bar
      WSL -> Windows:  /mnt/z/foo/bar  ->  Z:\\foo\\bar
    """
    import re
    if not path:
        return path
    # Normalise all separators to forward slashes first for easy matching.
    p = path.replace("\\", "/")
    if sys.platform != "win32":
        # Running on Linux/WSL — convert Windows drive paths to /mnt/<drive>/...
        m = re.match(r'^([A-Za-z]):/(.*)', p)
        if m:
            p = f"/mnt/{m.group(1).lower()}/{m.group(2)}"
    else:
        # Running on Windows — convert WSL mount paths to <drive>:\...
        m = re.match(r'^/mnt/([a-zA-Z])/(.*)', p)
        if m:
            p = f"{m.group(1).upper()}:/{m.group(2)}"
    return os.path.normpath(p)


def portable_path(path: str) -> str:
    """
    Convert a path to a portable storage form that is safe to write into JSON
    on any platform:
      - Forward slashes throughout (no backslashes to escape in JSON).
      - Windows drive letters kept as-is  (Z:/PARA/...)
      - WSL mount paths kept as-is        (/mnt/z/PARA/...)
    This is the inverse of normalize_path's OS-native conversion: use
    portable_path() when *saving* to JSON, normalize_path() when *reading*
    from JSON for actual filesystem access.
    """
    if not path:
        return path
    return path.replace("\\", "/")

# ---------------------------------------------------------------------------
# Argument Parsing & Logging
# ---------------------------------------------------------------------------

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"Music Score Viewer v{APP_VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose",   action="store_true", help="Show info messages.")
    parser.add_argument("-D", "--debug",     action="store_true", help="Show debug messages.")
    parser.add_argument("-l", "--log-file",  type=str,            help="Path to a log file.")
    parser.add_argument("-d", "--dir",       type=str,            help="Open directory on startup.")
    parser.add_argument("--no-last-dir",     action="store_true", help="Do not load the last used directory on startup.")
    args, _ = parser.parse_known_args()
    return args


def setup_logging(args: argparse.Namespace) -> None:
    handlers: list[logging.Handler] = []
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, mode='w'))
    else:
        handlers.append(logging.StreamHandler(sys.stdout))

    level = logging.WARNING
    if args.verbose:
        level = logging.INFO
    if args.debug:
        level = logging.DEBUG

    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers,
    )


args = parse_arguments()
setup_logging(args)

# ---------------------------------------------------------------------------
# Third-party imports (fail clearly)
# ---------------------------------------------------------------------------

try:
    import pymupdf as fitz
except ImportError:
    logging.critical("PyMuPDF not found. Run: pip install pymupdf")
    sys.exit(1)

try:
    from PIL import Image, ImageTk
except ImportError:
    logging.critical("Pillow not found. Run: pip install Pillow")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helper Classes
# ---------------------------------------------------------------------------

class SafeJSON:
    """Atomic JSON read/write to prevent data corruption on crash."""

    @staticmethod
    def load(filepath: str, default=None):
        if not os.path.exists(filepath):
            return default if default is not None else {}
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logging.error(f"Corrupt JSON in {filepath}: {e}")
            messagebox.showwarning(
                "Corrupt Data File",
                f"Could not parse:\n{filepath}\n\n{e}\n\n"
                "Starting with empty data. Your original file has not been changed."
            )
            return default if default is not None else {}
        except Exception as e:
            logging.error(f"Error reading JSON {filepath}: {e}")
            return default if default is not None else {}

    @staticmethod
    def save(filepath: str, data) -> bool:
        """Write data via a local temp file then move/copy to the destination.

        Writing the temp file locally (rather than in the target directory)
        avoids hangs when the target is on a network drive.  If the destination
        is on the same device, os.replace gives an atomic rename; otherwise
        shutil.copy2 is used as a fallback.

        Returns True on success, False on failure.  A visible error dialog is
        shown to the user on failure so callers do not need their own
        error-reporting logic.
        """
        tmp_name = None
        try:
            dir_name = os.path.dirname(filepath)
            if dir_name and not os.path.exists(dir_name):
                msg = f"Cannot save — directory does not exist:\n{dir_name}"
                logging.error(f"SafeJSON.save: {msg}")
                messagebox.showerror("Save Failed", msg)
                return False
            # Write to a local temp file to avoid blocking on network drives.
            fd, tmp_name = tempfile.mkstemp(text=True)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
            try:
                os.replace(tmp_name, filepath)
            except OSError:
                # Cross-device (e.g. local temp → network drive): copy data only.
                # shutil.copyfile avoids metadata ops that fail on SMB/CIFS mounts.
                shutil.copyfile(tmp_name, filepath)
                os.remove(tmp_name)
            tmp_name = None
            return True
        except Exception as e:
            msg = f"Failed to save:\n{filepath}\n\n{e}"
            logging.error(f"SafeJSON.save: {msg}")
            messagebox.showerror("Save Failed", msg)
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass
            return False


class ConfigManager:
    """Loads, deep-merges, and saves application configuration."""

    def __init__(self, filepath: str, defaults: dict) -> None:
        self.filepath = filepath
        self.defaults = defaults
        self.data = self._load_and_merge()

    def _load_and_merge(self) -> dict:
        loaded = SafeJSON.load(self.filepath, default={})
        merged = {k: (v.copy() if isinstance(v, dict) else v)
                  for k, v in self.defaults.items()}
        for k, v in loaded.items():
            if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k].update(v)
            else:
                merged[k] = v
        SafeJSON.save(self.filepath, merged)
        return merged

    def get(self, section: str, key: str = None, default=None):
        val = self.data.get(section)
        if key is None:
            return val if val is not None else default
        if isinstance(val, dict):
            return val.get(key, default)
        return default

    def set(self, section: str, key: str, value) -> None:
        if section not in self.data or not isinstance(self.data[section], dict):
            self.data[section] = {}
        self.data[section][key] = value
        SafeJSON.save(self.filepath, self.data)


# ---------------------------------------------------------------------------
# Score data model
# ---------------------------------------------------------------------------

class Score:
    """Represents a single PDF score parsed from a filename."""

    __slots__ = ['filepath', 'filename', 'composer', 'title', 'tags']

    def __init__(self, filepath: str, filename: str, folder_tags=None) -> None:
        self.filepath = normalize_path(filepath)
        self.filename = filename
        self.composer = "Unknown"
        self.title = ""
        self.tags: set[str] = set()
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


# ---------------------------------------------------------------------------
# Dialog: Text Annotation Entry
# ---------------------------------------------------------------------------

class TextEntryDialog(tk.Toplevel):
    """Modal dialog for creating or editing a text annotation."""

    def __init__(self, parent, title="Add Text", initial_color="black",
                 initial_text="", initial_font="New Century Schoolbook") -> None:
        super().__init__(parent)
        self.transient(parent)
        self.title(title)
        self.result = None

        available_fonts = sorted(tkfont.families())
        self.default_font = initial_font
        if initial_font not in available_fonts:
            for fallback in ("Arial", "Helvetica", "TkDefaultFont"):
                if fallback in available_fonts or fallback == "TkDefaultFont":
                    self.default_font = fallback
                    break

        self.update_idletasks()
        self.geometry(f"+{parent.winfo_rootx() + 50}+{parent.winfo_rooty() + 50}")
        self._setup_ui(initial_text, self.default_font)

        self.bind("<Return>", lambda e: self._on_ok())
        self.bind("<Escape>", lambda e: self.destroy())

        self.wait_visibility()
        self.grab_set()
        self.entry.focus_set()
        self.entry.icursor(tk.END)
        self.wait_window(self)

    def _setup_ui(self, initial_text: str, initial_font: str) -> None:
        f_input = tk.Frame(self, padx=10, pady=10)
        f_input.pack(fill=tk.X)
        tk.Label(f_input, text="Text:").pack(side=tk.LEFT)
        self.entry = tk.Entry(f_input, width=40)
        self.entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        if initial_text:
            self.entry.insert(0, initial_text)

        f_font = tk.Frame(self, padx=10, pady=5)
        f_font.pack(fill=tk.X)
        tk.Label(f_font, text="Font:").pack(side=tk.LEFT)
        fonts = sorted(tkfont.families())
        self.var_font = tk.StringVar(value=initial_font)
        self.combo_font = ttk.Combobox(f_font, textvariable=self.var_font,
                                       values=fonts, state="readonly")
        self.combo_font.pack(side=tk.LEFT, padx=5)

        f_sym = tk.LabelFrame(self, text="Symbols", padx=5, pady=5)
        f_sym.pack(fill=tk.X, padx=10, pady=5)

        symbols = [
            ("Minim",     "\U0001D15E"),
            ("Crotchet",  "♩"),
            ("D.Crotchet","♩."),
            ("Quaver",    "♪"),
            ("pp", "pp"), ("p", "p"), ("mp", "mp"),
            ("mf", "mf"), ("f", "f"), ("ff", "ff"),
        ]

        MAX_COLS = 5
        f_family = "Times New Roman" if "Times New Roman" in fonts else "TkDefaultFont"

        for i, (name, char) in enumerate(symbols):
            display = "𝅗𝅥" if "Minim" in name else char
            is_dyn = len(char) > 1 and char[0] in "mpf"
            f_style = tkfont.Font(
                family=f_family, size=14, weight="bold",
                slant="italic" if is_dyn else "roman",
            )
            btn = tk.Button(f_sym, text=display, font=f_style, width=4,
                            command=lambda ch=char: self._insert(ch))
            btn.grid(row=i // MAX_COLS, column=i % MAX_COLS, padx=2, pady=2, sticky="ew")

        f_btn = tk.Frame(self, padx=10, pady=10)
        f_btn.pack(fill=tk.X)
        tk.Button(f_btn, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_btn, text="OK", command=self._on_ok).pack(side=tk.RIGHT, padx=5)

    def _insert(self, char: str) -> None:
        self.entry.insert(tk.INSERT, char)
        self.entry.focus_set()

    def _on_ok(self) -> None:
        txt = self.entry.get()
        if txt:
            self.result = {"text": txt, "font": self.var_font.get()}
        self.destroy()


# ---------------------------------------------------------------------------
# Dialog: Score Picker (add to setlist)
# ---------------------------------------------------------------------------

class ScorePickerDialog(tk.Toplevel):
    """Modal dialog for selecting a score to add to a setlist."""

    def __init__(self, parent, scores: list) -> None:
        super().__init__(parent)
        self.transient(parent)
        self.title("Add Song to Setlist")
        self.scores = scores
        self.result = None

        self.geometry(f"800x600+{parent.winfo_rootx() + 50}+{parent.winfo_rooty() + 50}")
        self._setup_ui()
        self.wait_visibility()
        self.grab_set()
        self.ent_search.focus_set()
        self.wait_window(self)

    def _setup_ui(self) -> None:
        f_top = tk.Frame(self, padx=5, pady=5)
        f_top.pack(fill=tk.X)
        tk.Label(f_top, text="Search:").pack(side=tk.LEFT)
        self.ent_search = tk.Entry(f_top)
        self.ent_search.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.ent_search.bind("<KeyRelease>", self._filter)

        self.tree = ttk.Treeview(self, columns=("composer", "title"), show="headings")
        self.tree.heading("composer", text="Composer")
        self.tree.heading("title", text="Title")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        f_opts = tk.LabelFrame(self, text="Page Constraints (Optional)", padx=5, pady=5)
        f_opts.pack(fill=tk.X, padx=5, pady=5)
        tk.Label(f_opts, text="Start Page:").pack(side=tk.LEFT)
        self.ent_start = tk.Entry(f_opts, width=5)
        self.ent_start.pack(side=tk.LEFT, padx=5)
        self.ent_start.insert(0, "1")
        tk.Label(f_opts, text="End Page (Empty for End):").pack(side=tk.LEFT, padx=10)
        self.ent_end = tk.Entry(f_opts, width=5)
        self.ent_end.pack(side=tk.LEFT, padx=5)

        f_btn = tk.Frame(self, padx=5, pady=10)
        f_btn.pack(fill=tk.X)
        tk.Button(f_btn, text="Cancel",   command=self.destroy).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_btn, text="Add Song", command=self._on_add).pack(side=tk.RIGHT, padx=5)

        self._filter()

    def _filter(self, event=None) -> None:
        txt = self.ent_search.get().lower()
        for i in self.tree.get_children():
            self.tree.delete(i)
        # Use a numeric index as iid — filepaths contain spaces/special chars
        # that Tkinter's Treeview iid mechanism silently corrupts.
        self._filtered_scores = []
        for s in self.scores:
            if txt in s.title.lower() or txt in s.composer.lower():
                idx = len(self._filtered_scores)
                self.tree.insert("", tk.END, values=(s.composer, s.title), iid=str(idx))
                self._filtered_scores.append(s)

    def _on_add(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        score_obj = self._filtered_scores[int(sel[0])]
        path  = score_obj.filepath
        title = score_obj.title
        comp  = score_obj.composer

        try:
            sp = int(self.ent_start.get())
        except ValueError:
            logging.warning("Invalid start page; defaulting to 1.")
            sp = 1

        ep = None
        ep_txt = self.ent_end.get().strip()
        if ep_txt:
            try:
                ep = int(ep_txt)
            except ValueError:
                logging.warning(f"Invalid end page value '{ep_txt}'; treating as None.")

        self.result = {
            "path": portable_path(path), "composer": comp, "title": title,
            "start_page": sp, "end_page": ep,
        }
        self.destroy()


# ---------------------------------------------------------------------------
# Dialog: Quick-add current score to a setlist
# ---------------------------------------------------------------------------

class QuickAddSetlistDialog(tk.Toplevel):
    """Modal dialog for quickly adding the current score to an existing setlist."""

    def __init__(self, parent, setlist_names, current_page: int) -> None:
        super().__init__(parent)
        self.transient(parent)
        self.title("Add to Setlist")
        self.setlist_names = sorted(setlist_names)
        self.current_page = current_page
        self.result = None

        self.update_idletasks()
        self.geometry(f"+{parent.winfo_rootx() + 100}+{parent.winfo_rooty() + 100}")
        self._setup_ui()
        self.wait_visibility()
        self.grab_set()
        self.cb_setlist.focus_set()
        self.wait_window(self)

    def _setup_ui(self) -> None:
        f_main = tk.Frame(self, padx=10, pady=10)
        f_main.pack(fill=tk.BOTH, expand=True)

        tk.Label(f_main, text="Target Setlist:").grid(row=0, column=0, sticky='w', pady=5)
        self.cb_setlist = ttk.Combobox(f_main, values=self.setlist_names,
                                       state="readonly", width=30)
        self.cb_setlist.grid(row=0, column=1, sticky='ew', pady=5)
        if self.setlist_names:
            self.cb_setlist.current(0)

        tk.Label(f_main, text="Start Page:").grid(row=1, column=0, sticky='w', pady=5)
        self.ent_start = tk.Entry(f_main, width=10)
        self.ent_start.grid(row=1, column=1, sticky='w', pady=5)
        self.ent_start.insert(0, str(self.current_page))

        tk.Label(f_main, text="End Page (Optional):").grid(row=2, column=0, sticky='w', pady=5)
        self.ent_end = tk.Entry(f_main, width=10)
        self.ent_end.grid(row=2, column=1, sticky='w', pady=5)

        f_btn = tk.Frame(self, padx=10, pady=10)
        f_btn.pack(fill=tk.X)
        tk.Button(f_btn, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_btn, text="Add",    command=self._on_ok).pack(side=tk.RIGHT, padx=5)

    def _on_ok(self) -> None:
        s_name = self.cb_setlist.get()
        if not s_name:
            return

        try:
            sp = int(self.ent_start.get())
        except ValueError:
            logging.warning("Invalid start page; defaulting to 1.")
            sp = 1

        ep = None
        ep_txt = self.ent_end.get().strip()
        if ep_txt:
            try:
                ep = int(ep_txt)
            except ValueError:
                logging.warning(f"Invalid end page value '{ep_txt}'; treating as None.")

        self.result = {"setlist": s_name, "start": sp, "end": ep}
        self.destroy()


# ---------------------------------------------------------------------------
# Widget: Scrollable grid of tag checkboxes
# ---------------------------------------------------------------------------

class CompactTagFrame(tk.Frame):
    """
    A Frame containing a scrollable canvas of checkbuttons.
    Column count is dynamic based on available width and tag text length.
    """

    def __init__(self, master, callback, **kwargs) -> None:
        super().__init__(master, **kwargs)
        self.callback = callback
        self.vars: dict[str, tk.BooleanVar] = {}
        self.buttons: list[ttk.Checkbutton] = []
        self.all_tags: list[str] = []
        self.max_tag_width = 100

        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        self.frame  = tk.Frame(self.canvas)
        self.vsb    = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas_win = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")
        self.frame.bind("<Configure>",  self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_frame_configure(self, event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        if event.width > 1:
            self.canvas.itemconfig(self.canvas_win, width=event.width)
            self._reflow(event.width)

    def set_tags(self, all_tags, selected) -> None:
        for btn in self.buttons:
            btn.destroy()
        self.buttons = []
        self.vars = {}
        self.all_tags = sorted(all_tags, key=str.lower)

        if not self.all_tags:
            return

        font = tkfont.nametofont("TkDefaultFont")
        self.max_tag_width = max((font.measure(t) for t in self.all_tags), default=60) + 45

        for tag in self.all_tags:
            var = tk.BooleanVar(value=(tag in selected))
            self.vars[tag] = var
            btn = ttk.Checkbutton(self.frame, text=tag, variable=var, command=self.callback)
            self.buttons.append(btn)

        w = self.canvas.winfo_width()
        self._reflow(max(w, 1))

    def _reflow(self, width: int) -> None:
        if not self.buttons:
            return
        width = max(width, 50)
        cols = max(1, int(width // (self.max_tag_width + 4)))

        for btn in self.buttons:
            btn.grid_forget()
        for i, btn in enumerate(self.buttons):
            btn.grid(row=i // cols, column=i % cols, sticky='w', padx=2, pady=1)
        for i in range(cols):
            self.frame.grid_columnconfigure(i, weight=1)

    def get_selected(self) -> set:
        return {t for t, v in self.vars.items() if v.get()}


# ---------------------------------------------------------------------------
# Setlist session state
# ---------------------------------------------------------------------------

@dataclass
class SetlistSession:
    name:       str
    items:      list
    index:      int
    start_page: int
    end_page:   int


# ---------------------------------------------------------------------------
# Annotation helpers
# ---------------------------------------------------------------------------

def _rotate_annotation_coords(annotations: list, delta: int) -> None:
    """
    Rotate annotation coordinates in-place by delta degrees (90° increments).
    annotations: list of annotation dicts for a single page.
    """
    steps = (delta // 90) % 4

    def _rot_pt(nx, ny):
        for _ in range(steps):
            nx, ny = 1.0 - ny, nx
        return nx, ny

    for annot in annotations:
        if annot['type'] == 'ink':
            annot['points'] = [list(_rot_pt(nx, ny)) for nx, ny in annot['points']]
        elif annot['type'] == 'text':
            annot['x'], annot['y'] = _rot_pt(annot['x'], annot['y'])


# ---------------------------------------------------------------------------
# Annotation manager
# ---------------------------------------------------------------------------

class AnnotationManager:
    """Owns all annotation state and persistence for the current score."""

    UNDO_DEPTH = 20

    def __init__(self, default_pen_color: str = "black") -> None:
        self.annotations:    dict                = {}
        self.rotations:      dict[int, int]      = {}
        self._undo_stack:    dict[int, deque]    = {}
        self.tool:           str                 = "nav"
        self.pen_color:      str                 = default_pen_color
        self.current_stroke: list                = []
        self._path:          str | None          = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self, path: str) -> None:
        """Load annotations and rotations from the sidecar JSON for *path*."""
        base = os.path.splitext(path)[0] + ".json"
        self._path = base
        raw  = SafeJSON.load(base)
        self.annotations = {}
        self.rotations   = {}
        self._undo_stack  = {}
        needs_resave = False

        if "version" in raw:
            for p_str, deg in raw.get("rotations", {}).items():
                self.rotations[int(p_str)] = int(deg)
            for p, items in raw.get("pages", {}).items():
                clean = []
                for it in items:
                    if "uuid" not in it:
                        it["uuid"] = str(uuid.uuid4())
                        needs_resave = True
                    clean.append(it)
                self.annotations[int(p)] = clean
        elif raw:
            for p, items in raw.items():
                clean = []
                for it in items:
                    it["uuid"] = str(uuid.uuid4())
                    clean.append(it)
                self.annotations[int(p)] = clean
            needs_resave = True

        if needs_resave:
            logging.info(f"Migrating annotations to v{ANNOTATION_VERSION}: {base}")
            self.save()

    def save(self) -> None:
        """Save annotations and rotations to the sidecar JSON."""
        if not self._path:
            return
        rotations_to_save = {str(p): r for p, r in self.rotations.items() if r % 360 != 0}
        SafeJSON.save(self._path, {
            "version":   ANNOTATION_VERSION,
            "rotations": rotations_to_save,
            "pages":     self.annotations,
        })

    def clear(self) -> None:
        """Reset all state; called when a score is closed."""
        self.annotations  = {}
        self.rotations    = {}
        self._undo_stack  = {}
        self.current_stroke = []
        self._path        = None

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def add(self, pg: int, annot: dict) -> None:
        """Push undo snapshot, append annotation, and persist."""
        self.push_undo(pg)
        if pg not in self.annotations:
            self.annotations[pg] = []
        self.annotations[pg].append(annot)
        self.save()

    def erase_at(self, pg: int, x: int, y: int, canvas, layout: dict) -> bool:
        """
        Erase the annotation closest to (x, y) within a generous halo.
        Returns True if something was erased (caller should redraw).
        """
        for item in canvas.find_overlapping(x - 20, y - 20, x + 20, y + 20):
            tags = canvas.gettags(item)
            if 'bg' in tags:
                continue
            uid = next((t[5:] for t in tags if t.startswith("uuid_")), None)
            if uid and pg in self.annotations:
                self.push_undo(pg)
                self.annotations[pg] = [
                    a for a in self.annotations[pg] if a['uuid'] != uid
                ]
                self.save()
                canvas.delete(item)
                return True
        return False

    def push_undo(self, pg: int) -> None:
        """Snapshot the current annotation list for *pg* before modifying it."""
        if pg not in self._undo_stack:
            self._undo_stack[pg] = deque(maxlen=self.UNDO_DEPTH)
        self._undo_stack[pg].append(copy.deepcopy(self.annotations.get(pg, [])))

    def undo(self, pg: int) -> bool:
        """Restore the previous snapshot for *pg*. Returns True if successful."""
        stack = self._undo_stack.get(pg)
        if stack:
            self.annotations[pg] = stack.pop()
            self.save()
            return True
        return False

    def rotate_page_annotations(self, pg: int, delta: int) -> None:
        """
        Transform annotation coordinates for *pg* by *delta* degrees and
        update the stored rotation value.
        """
        if pg in self.annotations and self.annotations[pg]:
            _rotate_annotation_coords(self.annotations[pg], delta)
        old_rot = self.rotations.get(pg, 0)
        self.rotations[pg] = (old_rot + delta) % 360
        self.save()


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class MusicScoreApp:
    """Main application controller."""

    # --- Initialisation ---

    def __init__(self, root: tk.Tk, start_dir: str = None,
                 ignore_last_dir: bool = False) -> None:
        self.root = root
        self.config = ConfigManager(CONFIG_PATH, DEFAULT_CONFIG)

        # Always use the hardcoded constant — config may hold a stale version
        # from a previous install and should never override what's in the code.
        self.base_title = f"Music Score Viewer v{APP_VERSION}"
        self.root.title(self.base_title)

        geom = self.config.get("ui", "window_size", default=DEFAULT_WIN_SIZE)
        try:
            self.root.geometry(geom)
        except tk.TclError:
            self.root.geometry(DEFAULT_WIN_SIZE)

        self._maximize()

        self.bg_color      = self.config.get("ui", "bg_color",      default=BG_COLOR)
        self.toolbar_color = self.config.get("ui", "toolbar_color",  default=TOOLBAR_COLOR)

        # --- Library state ---
        self.scores:          list[Score] = []
        self._filtered_scores: list[Score] = []  # currently displayed subset
        self.sort_col:    str  = "composer"
        self.sort_desc:   bool = False
        self._filter_event_guard: bool = False  # replaces ignore_events

        # --- Viewer state ---
        self.doc               = None
        self.current_score_path: str | None = None
        self.current_page      = 0
        self.total_pages       = 0
        self.tk_image          = None
        self.is_two_page       = False
        self.page_layout: list = []
        self._resize_job       = None   # debounce handle

        # Annotation state (annotations, rotations, undo, tool, pen colour)
        self.annot = AnnotationManager(
            default_pen_color=self.config.get("ui", "default_pen_color", default="black")
        )

        # --- Setlist state ---
        self.library_dir: str = ""
        self.setlists            = {}   # loaded/reloaded when library_dir is set
        self._session: SetlistSession | None = None

        # --- Combo typeahead ---
        self.combo_search        = ""
        self.combo_last_key_time = 0.0

        # --- UI ---
        style = ttk.Style()
        style.configure("Treeview", rowheight=25)

        self.notebook   = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.f_library  = tk.Frame(self.notebook)
        self.f_setlists = tk.Frame(self.notebook)
        self.f_display  = tk.Frame(root, bg=self.bg_color)

        self.notebook.add(self.f_library,  text="Library")
        self.notebook.add(self.f_setlists, text="Setlists")

        self._setup_library_ui()
        self._setup_setlist_ui()
        self._setup_display_ui()
        self._bind_keys()

        # --- Initial directory ---
        initial_load = start_dir
        if not initial_load and not ignore_last_dir:
            last_dir = normalize_path(self.config.get("behavior", "last_directory", default=""))
            if last_dir:
                if os.path.exists(last_dir):
                    initial_load = last_dir
                else:
                    logging.info(f"Saved directory not found: {last_dir}")

        if initial_load:
            self.root.after(200, lambda: self._load_dir(initial_load))
        else:
            self.root.after(200, self._prompt_dir)

    def _maximize(self) -> None:
        """Maximise the window cross-platform, catching only Tcl errors."""
        try:
            self.root.attributes('-zoomed', True)   # Linux / X11
        except tk.TclError:
            try:
                self.root.state('zoomed')            # Windows
            except tk.TclError:
                pass                                 # macOS — just leave as-is

    # -----------------------------------------------------------------------
    # UI Setup: Library tab
    # -----------------------------------------------------------------------

    def _setup_library_ui(self) -> None:
        top = tk.Frame(self.f_library, padx=10, pady=10)
        top.pack(fill=tk.X)
        tk.Label(top, text="Search (Ctrl+F):").pack(side=tk.LEFT)
        self.ent_search = tk.Entry(top)
        self.ent_search.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.ent_search.bind("<KeyRelease>", self._on_filter)
        tk.Button(top, text="Folder",        command=self._prompt_dir).pack(side=tk.RIGHT, padx=5)
        tk.Button(top, text="Reset (Alt+R)", command=self._reset_filters).pack(side=tk.RIGHT)

        mid = tk.Frame(self.f_library, padx=10, pady=5, height=200)
        mid.pack(fill=tk.X)
        mid.pack_propagate(False)

        f_comp = tk.LabelFrame(mid, text="Composer (Alt+C)")
        f_comp.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        f_comp_row = tk.Frame(f_comp)
        f_comp_row.pack(fill=tk.X, padx=5, pady=5)

        self.cb_comp = ttk.Combobox(f_comp_row, state="readonly", width=25)
        self.cb_comp.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.cb_comp.bind("<<ComboboxSelected>>", self._on_filter)
        self.cb_comp.bind("<Key>", self._on_combo_type)

        tk.Button(f_comp_row, text="×", width=2, command=self._clear_composer_filter,
                  relief="flat", bg="#ddd").pack(side=tk.LEFT, padx=(2, 0))

        f_tags = tk.LabelFrame(mid, text="Tags")
        f_tags.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tag_grid = CompactTagFrame(f_tags, callback=self._on_filter)
        self.tag_grid.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        cols = ("composer", "title", "tags")
        self.tree = ttk.Treeview(self.f_library, columns=cols, show="headings")
        for c in cols:
            self.tree.heading(c, text=c.capitalize(),
                              command=lambda col=c: self._on_header_click(col))
        self.tree.column("composer", width=200)
        self.tree.column("title",    width=400)

        sb = ttk.Scrollbar(self.f_library, command=self.tree.yview)
        self.tree.configure(yscroll=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.tree.bind("<Double-1>", self._on_open)
        self.tree.bind("<Return>",   self._on_open)
        self._update_headers()

    # -----------------------------------------------------------------------
    # UI Setup: Setlists tab
    # -----------------------------------------------------------------------

    def _setup_setlist_ui(self) -> None:
        paned = tk.PanedWindow(self.f_setlists, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        f_left = tk.Frame(paned)
        tk.Label(f_left, text="My Setlists").pack(anchor="w")
        self.lst_setlists = tk.Listbox(f_left)
        self.lst_setlists.pack(fill=tk.BOTH, expand=True)
        self.lst_setlists.bind("<<ListboxSelect>>", self._on_setlist_select)

        f_lbtn = tk.Frame(f_left)
        f_lbtn.pack(fill=tk.X, pady=2)
        tk.Button(f_lbtn, text="New",    command=self._sl_new).pack(side=tk.LEFT)
        tk.Button(f_lbtn, text="Delete", command=self._sl_delete).pack(side=tk.RIGHT)
        tk.Button(f_lbtn, text="Rename", command=self._sl_rename).pack(side=tk.RIGHT)

        f_right = tk.Frame(paned)
        tk.Label(f_right, text="Songs").pack(anchor="w")
        cols = ("seq", "title", "pages")
        self.tree_sl = ttk.Treeview(f_right, columns=cols, show="headings")
        self.tree_sl.heading("seq",   text="#");      self.tree_sl.column("seq",   width=40)
        self.tree_sl.heading("title", text="Title");  self.tree_sl.column("title", width=300)
        self.tree_sl.heading("pages", text="Pages");  self.tree_sl.column("pages", width=100)
        self.tree_sl.pack(fill=tk.BOTH, expand=True)
        self.tree_sl.bind("<Double-1>", self._on_setlist_item_open)
        self.tree_sl.bind("<Return>",   self._on_setlist_item_open)

        f_rbtn = tk.Frame(f_right)
        f_rbtn.pack(fill=tk.X, pady=2)
        tk.Button(f_rbtn, text="Play Setlist", command=self._play_setlist,
                  bg="#ccffcc").pack(side=tk.RIGHT, padx=5)
        tk.Button(f_rbtn, text="Add Song...",  command=self._sl_add_song).pack(side=tk.LEFT)
        tk.Button(f_rbtn, text="Remove",       command=self._sl_remove_song).pack(side=tk.LEFT)
        tk.Button(f_rbtn, text="Move Up",      command=lambda: self._sl_move(-1)).pack(side=tk.LEFT)
        tk.Button(f_rbtn, text="Move Down",    command=lambda: self._sl_move(1)).pack(side=tk.LEFT)

        paned.add(f_left, width=200)
        paned.add(f_right)

        self._refresh_setlist_list()

    # -----------------------------------------------------------------------
    # UI Setup: Score viewer / display panel
    # -----------------------------------------------------------------------

    def _setup_display_ui(self) -> None:
        tb = tk.Frame(self.f_display, bg=self.toolbar_color, height=50)
        tb.pack(side=tk.TOP, fill=tk.X)
        tb.pack_propagate(False)

        tk.Label(tb, text="Tools:", bg=self.toolbar_color).pack(side=tk.LEFT, padx=5)

        # Keep references to tool buttons so we can highlight the active one
        self._tool_buttons: dict[str, tk.Button] = {}
        for label, tool_name in [("Nav", "nav"), ("Pen", "pen"),
                                  ("Text", "text"), ("Erase", "eraser")]:
            btn = tk.Button(tb, text=label, width=5,
                            command=lambda t=tool_name: self._set_tool(t))
            btn.pack(side=tk.LEFT, padx=2, pady=5)
            self._tool_buttons[tool_name] = btn

        tk.Frame(tb, width=1, bg="#999").pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=5)
        for c in ["black", "red", "blue", "green", "orange", "purple", "magenta"]:
            tk.Button(tb, bg=c, width=2,
                      command=lambda x=c: self._set_color(x)).pack(side=tk.LEFT, padx=1)

        tk.Frame(tb, width=1, bg="#999").pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=5)
        tk.Label(tb, text="Size:", bg=self.toolbar_color).pack(side=tk.LEFT)
        self.sc_size = tk.Scale(tb, from_=1, to=10, orient=tk.HORIZONTAL,
                                length=80, bg=self.toolbar_color)
        self.sc_size.set(self.config.get("ui", "default_pen_size", default=2))
        self.sc_size.pack(side=tk.LEFT, padx=5)

        # --- Rotation controls ---
        tk.Frame(tb, width=1, bg="#999").pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=5)
        tk.Label(tb, text="Rotate:", bg=self.toolbar_color).pack(side=tk.LEFT)
        tk.Button(tb, text="↺", width=2, font=("Arial", 12),
                  command=lambda: self._rotate_page(-90)).pack(side=tk.LEFT, padx=1, pady=5)
        tk.Button(tb, text="↻", width=2, font=("Arial", 12),
                  command=lambda: self._rotate_page(90)).pack(side=tk.LEFT, padx=1, pady=5)
        self.lbl_rotation = tk.Label(tb, text="0°", bg=self.toolbar_color,
                                     font=("Arial", 9), width=4)
        self.lbl_rotation.pack(side=tk.LEFT, padx=(2, 5))

        f_page = tk.Frame(tb, bg=self.toolbar_color)
        f_page.pack(side=tk.RIGHT, padx=10)

        tk.Button(f_page, text="Add to Setlist", command=self._quick_add_to_setlist,
                  bg="#e6f2ff").pack(side=tk.RIGHT, padx=(10, 0))

        self.lbl_total = tk.Label(f_page, text="/ -", bg=self.toolbar_color,
                                  font=("Arial", 10))
        self.lbl_total.pack(side=tk.RIGHT)

        self.var_page  = tk.StringVar()
        self.ent_page  = tk.Entry(f_page, textvariable=self.var_page,
                                  width=6, justify="center")
        self.ent_page.pack(side=tk.RIGHT, padx=2)
        self.ent_page.bind("<Return>", self._on_page_entry)

        tk.Label(f_page, text="Page:", bg=self.toolbar_color,
                 font=("Arial", 10)).pack(side=tk.RIGHT)

        self.lbl_status  = tk.Label(tb, text="Mode: Nav", bg=self.toolbar_color,
                                    font=("Arial", 10, "bold"))
        self.lbl_status.pack(side=tk.RIGHT, padx=10)
        self.lbl_col_ind = tk.Label(tb, width=3, bg=self.annot.pen_color)
        self.lbl_col_ind.pack(side=tk.RIGHT, padx=5)

        self.canvas = tk.Canvas(self.f_display, bg=self.bg_color, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<Button-1>",        self._on_click)
        self.canvas.bind("<B1-Motion>",        self._on_drag)
        self.canvas.bind("<ButtonRelease-1>",  self._on_release)
        self.f_display.bind("<Configure>",     self._on_resize)

        # Highlight the default tool
        self._update_tool_buttons()

    # -----------------------------------------------------------------------
    # Key bindings
    # -----------------------------------------------------------------------

    def _bind_keys(self) -> None:
        kb = self.config.get("keybindings", default={})

        def bind_list(action_name: str, func) -> None:
            for key in kb.get(action_name, []):
                try:
                    self.root.bind(key, func)
                except tk.TclError as e:
                    logging.warning(f"Could not bind '{key}' for '{action_name}': {e}")

        bind_list("close_score",     self._close_score)
        bind_list("next_page",       self._next_page)
        bind_list("prev_page",       self._prev_page)
        bind_list("first_page",      self._on_home)
        bind_list("last_page",       self._on_end)
        bind_list("search_focus",    lambda e: self.ent_search.focus_set())
        bind_list("filter_composer", lambda e: self.cb_comp.focus_set())
        bind_list("reset_filters",   lambda e: self._reset_filters())
        bind_list("go_to_page",      self._focus_page_entry)
        bind_list("undo",            lambda e: self._undo())
        bind_list("rotate_cw",       lambda e: self._rotate_page(90))
        bind_list("rotate_ccw",      lambda e: self._rotate_page(-90))

    def _should_ignore_key(self) -> bool:
        widget = self.root.focus_get()
        return isinstance(widget, (tk.Entry, ttk.Combobox))

    def _on_combo_type(self, event) -> str | None:
        if event.keysym in ('Return', 'Tab', 'Up', 'Down', 'Left', 'Right', 'Home', 'End'):
            return None
        now = time.time()
        if now - self.combo_last_key_time > COMBO_TYPEAHEAD_RESET_SECS:
            self.combo_search = ""
        self.combo_last_key_time = now
        if event.char and event.char.isprintable():
            self.combo_search += event.char.lower()
            for i, val in enumerate(self.cb_comp['values']):
                if val.lower().startswith(self.combo_search):
                    self.cb_comp.current(i)
                    self._on_filter()
                    return "break"
        return None

    def _focus_page_entry(self, event=None) -> None:
        if self.f_display.winfo_ismapped():
            self.ent_page.focus_set()
            self.ent_page.select_range(0, tk.END)

    # -----------------------------------------------------------------------
    # Library logic
    # -----------------------------------------------------------------------

    def _prompt_dir(self) -> None:
        self.root.lift()
        d = filedialog.askdirectory()
        if d:
            self._load_dir(d)

    def _load_dir(self, path: str) -> None:
        """Scan directory for PDFs in a background thread to keep UI responsive."""
        if not path or not os.path.exists(path):
            return

        self.root.config(cursor="watch")
        self.root.update_idletasks()
        logging.info(f"Scanning {path}")

        def _scan():
            found = []
            try:
                for root_dir, _, files in os.walk(path):
                    rel = os.path.normpath(os.path.relpath(root_dir, path))
                    parts = rel.lower().replace("\\", "/").split("/")
                    ftags = {p for p in parts if p and p != "."}
                    for f in files:
                        if f.lower().endswith(".pdf"):
                            found.append(Score(os.path.join(root_dir, f), f, ftags))
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror("Scan Error", str(exc)))
            finally:
                self.root.after(0, lambda: self._on_scan_complete(found, path))

        threading.Thread(target=_scan, daemon=True).start()

    def _on_scan_complete(self, found: list, path: str) -> None:
        self.scores = found
        self._apply_filters()
        self.config.set("behavior", "last_directory", portable_path(path))
        self.root.config(cursor="")
        self.library_dir = path
        self.setlists = SafeJSON.load(self._setlist_path())
        self._refresh_setlist_list()
        self._refresh_setlist_items()

    def _clear_composer_filter(self) -> None:
        self.cb_comp.set("")
        self._on_filter()

    def _reset_filters(self) -> None:
        self._filter_event_guard = True
        try:
            self.ent_search.delete(0, tk.END)
            self.cb_comp.set("")
            self._apply_filters(reset_tags=True)
        finally:
            self._filter_event_guard = False

    def _on_header_click(self, col: str) -> None:
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col  = col
            self.sort_desc = False
        self._update_headers()
        self._apply_filters()

    def _update_headers(self) -> None:
        for col in ("composer", "title", "tags"):
            text = col.capitalize()
            if col == self.sort_col:
                text += " ▼" if self.sort_desc else " ▲"
            self.tree.heading(col, text=text)

    def _on_filter(self, event=None) -> None:
        if not self._filter_event_guard:
            self._apply_filters()

    def _apply_filters(self, reset_tags: bool = False) -> None:
        """
        Filter and display the score list.  Guard flag is managed with
        try/finally so it can never be left stuck True by an exception.
        """
        self._filter_event_guard = True
        try:
            txt  = self.ent_search.get().lower()
            comp = self.cb_comp.get()
            if comp == "All Composers":
                comp = ""
            tags = set() if reset_tags else self.tag_grid.get_selected()

            matches = [
                s for s in self.scores
                if (not txt  or txt  in s.title.lower() or txt in s.composer.lower())
                and (not comp or s.composer == comp)
                and tags.issubset(s.tags)
            ]

            key_map = {
                'composer': lambda s: (s.composer.lower(), s.title.lower()),
                'title':    lambda s: (s.title.lower(),    s.composer.lower()),
                'tags':     lambda s: (sorted(s.tags),     s.composer.lower()),
            }
            if self.sort_col in key_map:
                matches.sort(key=key_map[self.sort_col], reverse=self.sort_desc)

            for x in self.tree.get_children():
                self.tree.delete(x)
            self._filtered_scores = matches
            for idx, s in enumerate(matches):
                t_str = ", ".join(sorted(s.tags))
                self.tree.insert("", tk.END, values=(s.composer, s.title, t_str),
                                 iid=str(idx))

            # Update available filter values (context-sensitive)
            av_comps: set[str] = set()
            av_tags:  set[str] = set()
            for s in self.scores:
                title_match = not txt or txt in s.title.lower() or txt in s.composer.lower()
                if title_match and tags.issubset(s.tags):
                    av_comps.add(s.composer)
                if title_match and (not comp or s.composer == comp) and tags.issubset(s.tags):
                    av_tags.update(s.tags)

            c_list = ["All Composers"] + sorted(av_comps)
            self.cb_comp['values'] = c_list
            if comp not in c_list:
                self.cb_comp.set("All Composers")
            self.tag_grid.set_tags(av_tags, tags)
        finally:
            self._filter_event_guard = False

    # -----------------------------------------------------------------------
    # Setlist logic
    # -----------------------------------------------------------------------

    def _refresh_setlist_list(self) -> None:
        self.lst_setlists.delete(0, tk.END)
        for name in sorted(self.setlists.keys()):
            self.lst_setlists.insert(tk.END, name)

    def _refresh_setlist_items(self) -> None:
        for x in self.tree_sl.get_children():
            self.tree_sl.delete(x)
        name = self._get_selected_setlist()
        if not name:
            return
        for i, item in enumerate(self.setlists.get(name, [])):
            sp  = item.get('start_page', 1)
            ep  = item.get('end_page')
            pg  = f"{sp}-{ep}" if ep else f"{sp}-End"
            title = f"{item.get('composer', '')} - {item.get('title', '')}"
            self.tree_sl.insert("", tk.END, values=(i + 1, title, pg), iid=i)

    def _get_selected_setlist(self) -> str | None:
        sel = self.lst_setlists.curselection()
        return self.lst_setlists.get(sel[0]) if sel else None

    def _on_setlist_select(self, event) -> None:
        self._refresh_setlist_items()

    def _sl_new(self) -> None:
        name = simpledialog.askstring("New Setlist", "Name:")
        if name and name not in self.setlists:
            self.setlists[name] = []
            self._save_setlists()
            self._refresh_setlist_list()
            sorted_names = sorted(self.setlists.keys())
            try:
                idx = sorted_names.index(name)
                self.lst_setlists.selection_clear(0, tk.END)
                self.lst_setlists.selection_set(idx)
                self.lst_setlists.activate(idx)
                self.lst_setlists.see(idx)
                self._refresh_setlist_items()
            except ValueError:
                pass

    def _sl_delete(self) -> None:
        name = self._get_selected_setlist()
        if name and messagebox.askyesno("Delete", f"Delete setlist '{name}'?"):
            del self.setlists[name]
            self._save_setlists()
            self._refresh_setlist_list()
            self._refresh_setlist_items()

    def _sl_rename(self) -> None:
        name = self._get_selected_setlist()
        if not name:
            return
        new_name = simpledialog.askstring("Rename", "New Name:", initialvalue=name)
        if new_name and new_name != name:
            self.setlists[new_name] = self.setlists.pop(name)
            self._save_setlists()
            self._refresh_setlist_list()

    def _sl_add_song(self) -> None:
        name = self._get_selected_setlist()
        if not name:
            messagebox.showwarning("Hint", "Select a setlist first.")
            return
        d = ScorePickerDialog(self.root, self.scores)
        if d.result:
            self.setlists[name].append(d.result)
            self._save_setlists()
            self._refresh_setlist_items()

    def _sl_remove_song(self) -> None:
        name = self._get_selected_setlist()
        sel  = self.tree_sl.selection()
        if name and sel:
            self.setlists[name].pop(int(sel[0]))
            self._save_setlists()
            self._refresh_setlist_items()

    def _sl_move(self, direction: int) -> None:
        name = self._get_selected_setlist()
        sel  = self.tree_sl.selection()
        if not name or not sel:
            return
        idx   = int(sel[0])
        items = self.setlists[name]
        new_idx = idx + direction
        if 0 <= new_idx < len(items):
            items[idx], items[new_idx] = items[new_idx], items[idx]
            self._save_setlists()
            self._refresh_setlist_items()
            self.tree_sl.selection_set(new_idx)

    def _setlist_path(self) -> str:
        """Return the setlists.json path inside the current music directory."""
        if self.library_dir:
            return os.path.join(self.library_dir, "setlists.json")
        return SETLIST_PATH

    def _save_setlists(self) -> None:
        SafeJSON.save(self._setlist_path(), self.setlists)

    def _quick_add_to_setlist(self) -> None:
        """Add currently viewed score to a setlist."""
        # FIX: guard against no score being open
        if not self.current_score_path:
            messagebox.showwarning("No Score", "Open a score before adding to a setlist.")
            return

        if not self.setlists:
            if messagebox.askyesno("No Setlists", "You have no setlists. Create one?"):
                self._sl_new()
            if not self.setlists:
                return

        score_obj = next((s for s in self.scores if s.filepath == self.current_score_path), None)
        title = score_obj.title    if score_obj else os.path.basename(self.current_score_path)
        comp  = score_obj.composer if score_obj else ""

        d = QuickAddSetlistDialog(self.root, self.setlists.keys(), self.current_page + 1)
        if d.result:
            s_name = d.result['setlist']
            self.setlists[s_name].append({
                "path":       portable_path(self.current_score_path),
                "composer":   comp,
                "title":      title,
                "start_page": d.result['start'],
                "end_page":   d.result['end'],
            })
            self._save_setlists()
            self._refresh_setlist_items()
            messagebox.showinfo("Success", f"Added to '{s_name}'")

    def _on_setlist_item_open(self, event=None) -> None:
        """Start the setlist from whichever item is currently selected in tree_sl."""
        name = self._get_selected_setlist()
        if not name:
            return
        items = self.setlists[name]
        if not items:
            messagebox.showinfo("Empty", "This setlist has no songs.")
            return
        sel = self.tree_sl.selection()
        start_index = int(sel[0]) if sel else 0
        self._session = SetlistSession(
            name=name, items=items, index=start_index, start_page=0, end_page=0
        )
        self._load_setlist_item(start_index)

    def _play_setlist(self) -> None:
        name  = self._get_selected_setlist()
        if not name:
            return
        items = self.setlists[name]
        if not items:
            messagebox.showinfo("Empty", "This setlist has no songs.")
            return
        self._session = SetlistSession(
            name=name, items=items, index=0, start_page=0, end_page=0
        )
        self._load_setlist_item(0)

    def _resolve_missing_file(self, item: dict, item_path: str, index: int, from_end: bool) -> str | None:
        """
        Called when a setlist item's file cannot be found.
        Offers three options:
          Skip   – advance to the next item (or stop at end of list).
          Locate – open a file dialog to manually find the file; updates the
                   stored path so future plays work without intervention.
          Cancel – exit setlist mode entirely.
        Returns the resolved path string on success, or None to abort.
        """
        title = item.get('title', os.path.basename(item_path))
        choice = self._missing_file_dialog(title, item_path)

        if choice == "skip":
            next_idx = index + 1
            if self._session and next_idx < len(self._session.items):
                self._load_setlist_item(next_idx, from_end)
            return None

        if choice == "locate":
            new_path = filedialog.askopenfilename(
                title=f"Locate: {title}",
                filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
                initialdir=os.path.dirname(item_path),
            )
            if new_path:
                new_path = normalize_path(new_path)
                # Update the stored path and save so future plays work.
                item['path'] = portable_path(new_path)
                self._save_setlists()
                return new_path
            # User cancelled the file dialog — treat as Cancel.

        # "cancel" or failed locate: exit setlist mode cleanly.
        self._session = None
        self.root.title(self.base_title)
        return None

    def _missing_file_dialog(self, title: str, path: str) -> str:
        """
        Modal dialog offering Skip / Locate / Cancel for a missing setlist file.
        Returns 'skip', 'locate', or 'cancel'.
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("File Not Found")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()

        tk.Label(dlg, text=f"Cannot find file for:\n\"{title}\"",
                 wraplength=380, justify=tk.LEFT).pack(padx=20, pady=(15, 5))
        tk.Label(dlg, text=path, wraplength=380, justify=tk.LEFT,
                 fg="grey40").pack(padx=20, pady=(0, 15))

        result = tk.StringVar(value="cancel")

        btn_frame = tk.Frame(dlg)
        btn_frame.pack(padx=20, pady=(0, 15))
        tk.Button(btn_frame, text="Skip",   width=10,
                  command=lambda: (result.set("skip"),   dlg.destroy())).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_frame, text="Locate…", width=10,
                  command=lambda: (result.set("locate"), dlg.destroy())).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_frame, text="Cancel", width=10,
                  command=lambda: (result.set("cancel"), dlg.destroy())).pack(side=tk.LEFT, padx=4)

        dlg.wait_window()
        return result.get()

    def _load_setlist_item(self, index: int, from_end: bool = False) -> None:
        if not self._session or not (0 <= index < len(self._session.items)):
            return
        self._session.index = index
        item = self._session.items[index]

        item_path = normalize_path(item['path'])
        if not os.path.exists(item_path):
            item_path = self._resolve_missing_file(item, item_path, index, from_end)
            if item_path is None:
                return
        if not self._load_pdf(item_path, setlist_mode=True):
            return

        sp = max(0, item.get('start_page', 1) - 1)
        ep_raw = item.get('end_page')
        ep = (ep_raw - 1) if ep_raw else (self.total_pages - 1)

        self._session.start_page = sp
        self._session.end_page   = min(ep, self.total_pages - 1)

        self._goto_page(self._session.end_page if from_end else self._session.start_page)
        self.root.title(
            f"[{self._session.name} ({index + 1}/"
            f"{len(self._session.items)})] {item.get('title', '')}"
        )

    # -----------------------------------------------------------------------
    # PDF loading and closing
    # -----------------------------------------------------------------------

    def _on_open(self, event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        self._session = None
        score = self._filtered_scores[int(sel[0])]
        self._load_pdf(score.filepath)

    def _load_pdf(self, path: str, setlist_mode: bool = False) -> bool:
        path = normalize_path(path)
        try:
            # FIX: always close the old document before opening a new one
            if self.doc:
                self.doc.close()
                self.doc = None

            self.doc               = fitz.open(path)
            self.current_score_path = path
            self.total_pages       = self.doc.page_count
            self.current_page      = 0

            if not setlist_mode:
                self.root.title(f"{self.base_title} - {os.path.basename(path)}")

            self.annot.load(path)

            self.notebook.pack_forget()
            self.f_display.pack(fill=tk.BOTH, expand=True)
            self._set_tool("nav")
            self.root.update_idletasks()
            self._render_pdf()
            return True
        except Exception as exc:
            messagebox.showerror("PDF Error", str(exc))
            return False

    def _close_score(self, event=None) -> None:
        if not self.f_display.winfo_ismapped():
            return
        # FIX: close the document handle before discarding the reference
        if self.doc:
            self.doc.close()
            self.doc = None

        self.current_score_path = None
        self.f_display.pack_forget()
        self.root.title(self.base_title)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        if self._session:
            self.tree_sl.focus_set()
        else:
            self.tree.focus_set()

    # -----------------------------------------------------------------------
    # Navigation
    # -----------------------------------------------------------------------

    def _goto_page(self, p: int) -> None:
        if self.doc and 0 <= p < self.total_pages:
            self.current_page = p
            self._render_pdf()

    def _next_page(self, e=None) -> None:
        if self._should_ignore_key() or not self.doc or self.annot.tool == "text":
            return
        step    = 2 if self.is_two_page else 1
        next_pg = self.current_page + step

        if self._session:
            at_end = (self.current_page >= self._session.end_page or
                      (self.is_two_page and self.current_page + 1 >= self._session.end_page))
            if at_end:
                if self._session.index < len(self._session.items) - 1:
                    self._load_setlist_item(self._session.index + 1)
                return
            if next_pg > self._session.end_page:
                return

        if next_pg < self.total_pages:
            self._goto_page(next_pg)

    def _prev_page(self, e=None) -> None:
        if self._should_ignore_key() or not self.doc or self.annot.tool == "text":
            return
        step    = 2 if self.is_two_page else 1
        prev_pg = self.current_page - step

        if self._session:
            if self.current_page <= self._session.start_page:
                if self._session.index > 0:
                    self._load_setlist_item(self._session.index - 1, from_end=True)
                return
            prev_pg = max(prev_pg, self._session.start_page)

        self._goto_page(max(0, prev_pg))

    def _on_home(self, e=None) -> None:
        if self._should_ignore_key():
            return
        target = self._session.start_page if self._session else 0
        self._goto_page(target)

    def _on_end(self, e=None) -> None:
        if self._should_ignore_key():
            return
        target = self._session.end_page if self._session else self.total_pages - 1
        self._goto_page(target)

    # -----------------------------------------------------------------------
    # Tool & colour selection
    # -----------------------------------------------------------------------

    def _set_tool(self, t: str) -> None:
        self.annot.tool = t
        self.lbl_status.config(text=f"Mode: {t.capitalize()}")
        cursors = {"nav": "", "pen": "pencil", "text": "xterm", "eraser": "crosshair"}
        self.canvas.config(cursor=cursors.get(t, ""))
        self._update_tool_buttons()

    def _update_tool_buttons(self) -> None:
        """Highlight the active tool button; reset all others."""
        for tool_name, btn in self._tool_buttons.items():
            if tool_name == self.annot.tool:
                btn.config(relief=tk.SUNKEN, bg="#aad4f5")
            else:
                btn.config(relief=tk.RAISED, bg=self.toolbar_color)

    def _set_color(self, c: str) -> None:
        """Set the pen colour without changing the active tool."""
        self.annot.pen_color = c
        self.lbl_col_ind.config(bg=c)
        # FIX: removed implicit tool switch — colour selection is independent of tool

    # -----------------------------------------------------------------------
    # Canvas interaction
    # -----------------------------------------------------------------------

    def _get_layout_at(self, x: int, y: int):
        for l in self.page_layout:
            if l['x'] <= x <= l['x'] + l['w'] and l['y'] <= y <= l['y'] + l['h']:
                return l
        return None

    def _on_click(self, event) -> None:
        if self.annot.tool == "nav":
            h = self.canvas.winfo_height()
            w = self.canvas.winfo_width()
            if event.y < h * 0.15:
                self._close_score()
            elif event.x < w / 2:
                self._prev_page()
            else:
                self._next_page()
            return

        l = self._get_layout_at(event.x, event.y)
        if not l:
            return

        if self.annot.tool == "pen":
            self.annot.current_stroke = [(event.x, event.y)]

        elif self.annot.tool == "text":
            item = self.canvas.find_closest(event.x, event.y, halo=5)
            tags = self.canvas.gettags(item)
            target_uuid = next((t[5:] for t in tags if t.startswith("uuid_")), None)

            edit_data = None
            if target_uuid and l['p'] in self.annot.annotations:
                edit_data = next(
                    (a for a in self.annot.annotations[l['p']]
                     if a['uuid'] == target_uuid and a['type'] == 'text'),
                    None,
                )

            if edit_data:
                d = TextEntryDialog(self.root, "Edit Text",
                                    edit_data['color'], edit_data['text'],
                                    edit_data.get('font', ''))
                if d.result:
                    self.annot.push_undo(l['p'])
                    edit_data.update({
                        "text":  d.result['text'],
                        "font":  d.result['font'],
                        "size":  self.sc_size.get(),
                        "color": self.annot.pen_color,
                    })
                    self.annot.save()
                    self._draw_vectors()
            else:
                d = TextEntryDialog(self.root, initial_color=self.annot.pen_color)
                if d.result:
                    nx = (event.x - l['x']) / l['w']
                    ny = (event.y - l['y']) / l['h']
                    annot = {
                        "uuid":  str(uuid.uuid4()),
                        "type":  "text",
                        "x":     nx, "y": ny,
                        "text":  d.result['text'],
                        "font":  d.result['font'],
                        "color": self.annot.pen_color,
                        "size":  self.sc_size.get(),
                    }
                    self.annot.add(l['p'], annot)
                    self._draw_vectors()

        elif self.annot.tool == "eraser":
            if self.annot.erase_at(l['p'], event.x, event.y, self.canvas, l):
                self._draw_vectors()

    def _on_drag(self, event) -> None:
        if self.annot.tool == "eraser":
            l = self._get_layout_at(event.x, event.y)
            if l:
                if self.annot.erase_at(l['p'], event.x, event.y, self.canvas, l):
                    self._draw_vectors()
            return
        if self.annot.tool == "pen" and self.annot.current_stroke:
            self.annot.current_stroke.append((event.x, event.y))
            pts = self.annot.current_stroke[-2:]
            w   = self.sc_size.get()
            self.canvas.create_line(
                pts[0][0], pts[0][1], pts[1][0], pts[1][1],
                fill=self.annot.pen_color, width=w, capstyle=tk.ROUND, joinstyle=tk.ROUND,
                tags="preview",
            )

    def _on_release(self, event) -> None:
        if self.annot.tool == "pen" and len(self.annot.current_stroke) > 1:
            l = self._get_layout_at(
                self.annot.current_stroke[0][0], self.annot.current_stroke[0][1]
            )
            if l:
                norm = [
                    [(sx - l['x']) / l['w'], (sy - l['y']) / l['h']]
                    for sx, sy in self.annot.current_stroke
                ]
                annot = {
                    "uuid":   str(uuid.uuid4()),
                    "type":   "ink",
                    "points": norm,
                    "color":  self.annot.pen_color,
                    "width":  self.sc_size.get(),
                }
                self.annot.add(l['p'], annot)
                self._draw_vectors()
        self.annot.current_stroke = []

    # -----------------------------------------------------------------------
    # Undo
    # -----------------------------------------------------------------------

    def _undo(self) -> None:
        if self._should_ignore_key():
            return
        if self.annot.undo(self.current_page):
            self._draw_vectors()

    # -----------------------------------------------------------------------
    # Resize (debounced) and page entry
    # -----------------------------------------------------------------------

    def _on_resize(self, event) -> None:
        """Debounce resize events — only re-render after the window stops moving."""
        if self.doc:
            if self._resize_job:
                self.root.after_cancel(self._resize_job)
            self._resize_job = self.root.after(RESIZE_DEBOUNCE_MS, self._render_pdf)

    def _on_page_entry(self, event) -> None:
        try:
            txt      = self.var_page.get().split('-')[0].strip()
            page_num = int(txt)
            if 1 <= page_num <= self.total_pages:
                self._goto_page(page_num - 1)
                self.canvas.focus_set()
            else:
                self._render_pdf()
        except ValueError:
            self._render_pdf()

    # -----------------------------------------------------------------------
    # Rendering
    # -----------------------------------------------------------------------

    def _render_pdf(self) -> None:
        if not self.doc:
            return
        win_w = self.canvas.winfo_width()
        win_h = self.canvas.winfo_height()
        if win_w < 10:
            win_w, win_h = 1200, 850

        p1       = self.doc.load_page(self.current_page)
        rot1     = self.annot.rotations.get(self.current_page, 0)
        r1       = p1.rect
        # For 90°/270° the page dimensions are transposed after rotation
        rot1_norm = rot1 % 360
        if rot1_norm in (90, 270):
            eff_w1, eff_h1 = r1.height, r1.width
        else:
            eff_w1, eff_h1 = r1.width, r1.height

        zoom_fit_h      = win_h / eff_h1
        sep             = 4
        width_two_pages = (eff_w1 * zoom_fit_h * 2) + sep
        self.is_two_page = (
            width_two_pages <= win_w and
            self.current_page + 1 < self.total_pages
        )
        if self._session and self.is_two_page:
            if self.current_page + 1 > self._session.end_page:
                self.is_two_page = False

        self.page_layout = []

        if self.is_two_page:
            rot2  = self.annot.rotations.get(self.current_page + 1, 0)
            zoom  = zoom_fit_h
            mat1  = fitz.Matrix(zoom, zoom).prerotate(rot1)
            mat2  = fitz.Matrix(zoom, zoom).prerotate(rot2)
            pix1  = p1.get_pixmap(matrix=mat1)
            p2    = self.doc.load_page(self.current_page + 1)
            pix2  = p2.get_pixmap(matrix=mat2)
            total_w = pix1.width + pix2.width + sep
            max_h   = max(pix1.height, pix2.height)
            x_off   = (win_w - total_w) // 2
            y_off   = (win_h - max_h)   // 2
            img = Image.new("RGB", (total_w, max_h), (0, 0, 0))
            img.paste(Image.frombytes("RGB", [pix1.width, pix1.height], pix1.samples), (0, 0))
            img.paste(Image.frombytes("RGB", [pix2.width, pix2.height], pix2.samples),
                      (pix1.width + sep, 0))
            self.page_layout = [
                {"p": self.current_page,     "x": x_off,                    "y": y_off,
                 "w": pix1.width,            "h": pix1.height,              "rot": rot1},
                {"p": self.current_page + 1, "x": x_off + pix1.width + sep, "y": y_off,
                 "w": pix2.width,            "h": pix2.height,              "rot": rot2},
            ]
            self.tk_image = ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            self.canvas.create_image(x_off, y_off, image=self.tk_image, anchor="nw", tags="bg")
            self.var_page.set(f"{self.current_page + 1}-{self.current_page + 2}")
        else:
            zoom  = min(win_w / eff_w1, win_h / eff_h1)
            mat   = fitz.Matrix(zoom, zoom).prerotate(rot1)
            pix1  = p1.get_pixmap(matrix=mat)
            img   = Image.frombytes("RGB", [pix1.width, pix1.height], pix1.samples)
            x_off = (win_w - pix1.width)  // 2
            y_off = (win_h - pix1.height) // 2
            self.page_layout = [{"p": self.current_page, "x": x_off, "y": y_off,
                                  "w": pix1.width, "h": pix1.height, "rot": rot1}]
            self.tk_image = ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            self.canvas.create_image(x_off, y_off, image=self.tk_image, anchor="nw", tags="bg")
            self.var_page.set(str(self.current_page + 1))

        self.lbl_total.config(text=f"/ {self.total_pages}")
        self.lbl_rotation.config(text=f"{rot1_norm}°")
        self._draw_vectors()

    def _draw_vectors(self) -> None:
        self.canvas.delete("annot")
        self.canvas.delete("preview")
        for layout in self.page_layout:
            pg = layout['p']
            if pg in self.annot.annotations:
                for annot in self.annot.annotations[pg]:
                    self._draw_single_annot(annot, layout)

    def _draw_single_annot(self, annot: dict, layout: dict) -> None:
        ox, oy, w, h = layout['x'], layout['y'], layout['w'], layout['h']
        rot  = layout.get('rot', 0) % 360
        tag  = ("annot", f"uuid_{annot['uuid']}")

        def _transform_pt(nx: float, ny: float):
            """
            Convert normalised page coords (0-1) to canvas pixel coords,
            accounting for the page's display rotation.  The rotation recorded
            in 'rot' is what PyMuPDF has already applied to the rendered bitmap,
            so annotation coordinates (stored in the *original* page space) must
            be rotated to match before being scaled to the bitmap dimensions.
            """
            if rot == 90:
                nx, ny = ny, 1.0 - nx
            elif rot == 180:
                nx, ny = 1.0 - nx, 1.0 - ny
            elif rot == 270:
                nx, ny = 1.0 - ny, nx
            return ox + nx * w, oy + ny * h

        if annot['type'] == 'ink':
            pts = []
            for nx, ny in annot['points']:
                cx, cy = _transform_pt(nx, ny)
                pts.extend([cx, cy])
            if len(pts) >= 4:
                self.canvas.create_line(
                    pts, fill=annot['color'], width=annot.get('width', 2),
                    capstyle=tk.ROUND, joinstyle=tk.ROUND, tags=tag,
                )
        elif annot['type'] == 'text':
            cx, cy = _transform_pt(annot['x'], annot['y'])
            txt = annot['text']
            fam = annot.get('font', 'Arial')
            sz  = int(12 + annot.get('size', 2) * 4)
            if txt.strip() in MUSICAL_SYMBOLS_SET:
                sz = int(sz * 6.0)
            self.canvas.create_text(
                cx, cy, text=txt, fill=annot['color'],
                font=(fam, sz), anchor="w", tags=tag,
            )

    # -----------------------------------------------------------------------
    # Rotation
    # -----------------------------------------------------------------------

    def _rotate_page(self, delta: int) -> None:
        """
        Rotate the current page by *delta* degrees (positive = clockwise).
        Existing annotations are coordinate-transformed so they remain correctly
        placed after the rotation.  The new rotation is saved to the sidecar.
        """
        if not self.doc:
            return
        if self._should_ignore_key():
            return
        self.annot.rotate_page_annotations(self.current_page, delta)
        self._render_pdf()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        root = tk.Tk()
        app  = MusicScoreApp(root, start_dir=args.dir, ignore_last_dir=args.no_last_dir)
        root.mainloop()
    except Exception as exc:
        logging.critical(f"App crash: {exc}", exc_info=True)
