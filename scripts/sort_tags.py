#!/usr/bin/env python3
"""Sort filename tags alphabetically for all PDFs in a music library.

Walks the given directory recursively, finds PDF files using the Folio
naming convention (``Composer - Title -- tag1 tag2.pdf``), and renames
any file whose tags are not already in sorted order.  Annotation sidecar
JSONs are moved alongside the PDF.  The ``_hash_index.json`` and
``setlists.json`` files are updated to reflect the new paths.

Usage:
    python3 scripts/sort_tags.py /path/to/library          # dry-run (default)
    python3 scripts/sort_tags.py /path/to/library --apply   # rename files

The dry-run mode shows what would be renamed without touching anything.
"""

import argparse
import json
import os
import sys

# Allow importing from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from web.core import (
    SafeJSON,
    annotation_sidecar_path,
    build_tagged_filename,
    normalize_path,
    portable_path,
)


def find_unsorted_pdfs(library_path: str) -> list[tuple[str, str, str]]:
    """Return a list of (dir, old_filename, new_filename) for unsorted PDFs."""
    results: list[tuple[str, str, str]] = []
    for root_dir, _subdirs, files in os.walk(library_path):
        for fname in files:
            if not fname.lower().endswith(".pdf"):
                continue
            base = os.path.splitext(fname)[0]
            if " -- " not in base:
                continue

            name_part, tag_part = base.split(" -- ", 1)
            tags = [t for t in tag_part.split() if t]
            if not tags:
                continue

            sorted_tags = sorted(tags)
            if tags == sorted_tags:
                continue

            # Parse composer/title to rebuild filename properly
            if " - " in name_part:
                parts = name_part.split(" - ", 1)
                composer = parts[0].strip()
                title = parts[1].strip()
            else:
                composer = "Unknown"
                title = name_part.strip()

            new_fname = build_tagged_filename(
                composer, title, set(tags), os.path.splitext(fname)[1]
            )
            results.append((root_dir, fname, new_fname))
    return results


def update_hash_index(
    library_path: str,
    renames: list[tuple[str, str, str]],
) -> int:
    """Update _hash_index.json entries to reflect renamed files.

    Returns the number of entries updated.
    """
    index_path = os.path.join(library_path, "_hash_index.json")
    if not os.path.exists(index_path):
        return 0

    index = SafeJSON.load(index_path, default={})
    count = 0
    for dir_path, old_name, new_name in renames:
        old_full = portable_path(os.path.join(dir_path, old_name))
        new_full = portable_path(os.path.join(dir_path, new_name))
        for content_hash, stored_path in list(index.items()):
            if stored_path == old_full:
                index[content_hash] = new_full
                count += 1
    if count:
        SafeJSON.save(index_path, index)
    return count


def update_setlists(
    library_path: str,
    renames: list[tuple[str, str, str]],
) -> int:
    """Update setlists.json entries to reflect renamed files.

    Returns the number of song path entries updated.
    """
    setlists_path = os.path.join(library_path, "setlists.json")
    if not os.path.exists(setlists_path):
        return 0

    # Build a lookup from old portable path -> new portable path
    path_map: dict[str, str] = {}
    for dir_path, old_name, new_name in renames:
        old_full = portable_path(os.path.join(dir_path, old_name))
        new_full = portable_path(os.path.join(dir_path, new_name))
        path_map[old_full] = new_full

    data = SafeJSON.load(setlists_path, default={})
    count = 0
    for setlist_name, items in data.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            path = item.get("path", "")
            if path in path_map:
                item["path"] = path_map[path]
                count += 1
    if count:
        SafeJSON.save(setlists_path, data)
    return count


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
        print("All PDF tags are already in alphabetical order.")
        return

    print(f"Found {len(unsorted)} file(s) with unsorted tags:\n")
    for dir_path, old_name, new_name in unsorted:
        rel = os.path.relpath(dir_path, library_path)
        prefix = "" if rel == "." else rel + os.sep
        print(f"  {prefix}{old_name}")
        print(f"  -> {prefix}{new_name}\n")

    if not args.apply:
        print("Dry run — no files were changed. Use --apply to rename.")
        return

    # Perform renames
    errors = 0
    applied: list[tuple[str, str, str]] = []
    for dir_path, old_name, new_name in unsorted:
        old_path = os.path.join(dir_path, old_name)
        new_path = os.path.join(dir_path, new_name)

        if os.path.exists(new_path):
            print(f"  SKIP (target exists): {new_name}")
            errors += 1
            continue

        # Rename PDF
        os.rename(old_path, new_path)

        # Rename annotation sidecar if it exists
        old_sidecar = annotation_sidecar_path(old_path)
        if os.path.exists(old_sidecar):
            new_sidecar = annotation_sidecar_path(new_path)
            os.rename(old_sidecar, new_sidecar)

        applied.append((dir_path, old_name, new_name))
        print(f"  Renamed: {old_name} -> {new_name}")

    # Update hash index and setlists
    if applied:
        hi = update_hash_index(library_path, applied)
        sl = update_setlists(library_path, applied)
        print(f"\nRenamed {len(applied)} file(s).")
        if hi:
            print(f"Updated {hi} hash index entry/entries.")
        if sl:
            print(f"Updated {sl} setlist reference(s).")

    if errors:
        print(f"\n{errors} file(s) skipped due to conflicts.")


if __name__ == "__main__":
    main()
