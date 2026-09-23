#!/usr/bin/env python3
"""Sort filename tags alphabetically for all PDFs in a music library.

Walks the given directory recursively, finds PDF files using the Folio
naming convention (``Composer - Title -- tag1 tag2.pdf``), and renames
any file whose tags are not in the app's canonical form (lowercase,
de-duplicated, sorted).  Annotation sidecar JSONs are moved alongside the
PDF.  ``_hash_index.json``, ``setlists.json`` and ``_recent.json`` are
updated to reflect the new paths.  All three are loaded before any rename,
so a corrupt one stops the run with nothing changed.  A reference file that
can't be saved afterwards is reported and the others are still saved.  Exits
non-zero if a reference file is corrupt or unsavable, or any file could not
be renamed.

Usage:
    python3 scripts/sort_tags.py /path/to/library          # dry-run (default)
    python3 scripts/sort_tags.py /path/to/library --apply   # rename files

The dry-run mode shows what would be renamed without touching anything.
"""

import argparse
import os
import sys
from typing import Callable

# Allow importing from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from web.core import (
    SafeJSON,
    SafeJSONError,
    Score,
    build_tagged_filename,
    load_hash_index,
    load_recent,
    load_setlists,
    normalize_path,
    portable_path,
    remap_hash_index,
    remap_recent_paths,
    remap_setlist_paths,
    rename_score_file,
)


def find_unsorted_pdfs(library_path: str) -> list[tuple[Score, str]]:
    """Return (score, new_filename) for each PDF whose tags aren't canonical.

    Canonical is what the app itself writes: lowercase, de-duplicated and
    sorted (see build_tagged_filename).
    """
    results: list[tuple[Score, str]] = []
    for root_dir, _subdirs, files in os.walk(library_path):
        for fname in files:
            base, ext = os.path.splitext(fname)
            if ext.lower() != ".pdf" or " -- " not in base:
                continue
            score = Score(os.path.join(root_dir, fname), fname)
            if not score.filename_tags:
                continue
            tag_part = base.split(" -- ", 1)[1]
            if tag_part == " ".join(sorted(score.filename_tags)):
                continue
            new_fname = build_tagged_filename(
                score.composer, score.title, score.filename_tags, ext
            )
            results.append((score, new_fname))
    return results


# (label, path, loaded data, remap function) for each file that stores paths
References = list[tuple[str, str, object, Callable[[object, dict[str, str]], int]]]


def load_references(library_path: str) -> References:
    """Load every file that stores score paths.

    Raises SafeJSONError if any is corrupt, so the caller can stop before
    renaming anything.
    """
    index = os.path.join(library_path, "_hash_index.json")
    setlists = os.path.join(library_path, "setlists.json")
    recent = os.path.join(library_path, "_recent.json")
    return [
        ("hash index", index, load_hash_index(index), remap_hash_index),
        ("setlist", setlists, load_setlists(setlists), remap_setlist_paths),
        ("recent-list", recent, load_recent(recent), remap_recent_paths),
    ]


def update_references(
    refs: References, remap: dict[str, str],
) -> tuple[dict[str, int], dict[str, SafeJSONError]]:
    """Apply *remap* to the loaded references and save those that changed.

    A failed save doesn't stop the others. Returns ({label: entries
    updated}, {path: error} for each file that could not be saved).
    """
    counts: dict[str, int] = {}
    failed: dict[str, SafeJSONError] = {}
    for label, path, data, remap_fn in refs:
        counts[label] = remap_fn(data, remap)
        if counts[label]:
            try:
                SafeJSON.save(path, data)
            except SafeJSONError as e:
                failed[path] = e
    return counts, failed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sort filename tags alphabetically for all PDFs."
    )
    parser.add_argument("library", help="Path to the music library directory")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually rename files (default is dry-run)",
    )
    args = parser.parse_args()

    library_path = normalize_path(args.library)
    if not os.path.isdir(library_path):
        print(f"Error: not a directory: {library_path}", file=sys.stderr)
        sys.exit(1)

    unsorted = find_unsorted_pdfs(library_path)

    if not unsorted:
        print("All PDF tags are already in canonical order.")
        return

    print(f"Found {len(unsorted)} file(s) with unsorted tags:\n")
    for score, new_name in unsorted:
        rel = os.path.relpath(os.path.dirname(score.filepath), library_path)
        prefix = "" if rel == "." else rel + os.sep
        print(f"  {prefix}{score.filename}")
        print(f"  -> {prefix}{new_name}\n")

    if not args.apply:
        print("Dry run — no files were changed. Use --apply to rename.")
        return

    # Load every reference file up front: if one is corrupt, stop before any
    # rename, rather than leave files renamed and references half-updated.
    try:
        refs = load_references(library_path)
    except SafeJSONError as e:
        print(f"Error: {e}\nNo files were renamed.", file=sys.stderr)
        sys.exit(1)

    # Perform renames (PDF + sidecar, rolled back together on failure)
    errors = 0
    remap: dict[str, str] = {}
    for score, new_name in unsorted:
        try:
            new_score = rename_score_file(score, new_name)
        except FileExistsError:
            print(f"  SKIP (target exists): {new_name}")
            errors += 1
            continue
        except OSError as e:
            print(f"  FAILED: {score.filename}: {e}", file=sys.stderr)
            errors += 1
            continue
        remap[portable_path(score.filepath)] = portable_path(new_score.filepath)
        print(f"  Renamed: {score.filename} -> {new_name}")

    # Update every stored reference to the renamed files
    if remap:
        print(f"\nRenamed {len(remap)} file(s).")
        counts, failed = update_references(refs, remap)
        for kind, count in counts.items():
            if count:
                print(f"Updated {count} {kind} entry/entries.")
        for path, e in failed.items():
            print(f"  FAILED to save {path}: {e}\n"
                  "  It still points at the old filenames.", file=sys.stderr)
        errors += len(failed)

    if errors:
        print(f"\n{errors} problem(s): see messages above.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
