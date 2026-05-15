#!/usr/bin/env python3
"""Migrate stored per-page rotations after the viewer rotation fix.

Background
----------
Before the fix, ``viewer.js`` passed only the user's stored rotation to
PDF.js's ``getViewport({rotation})``, which OVERRIDES the page's intrinsic
``/Rotate`` metadata rather than adding to it.  Users who manually rotated
pages to compensate for an intrinsic non-zero rotation ended up with a
stored rotation equal to that intrinsic value.

After the fix, the renderer combines intrinsic + stored rotation, so the
old compensation is double-applied.  This script subtracts each page's
intrinsic ``/Rotate`` from its stored rotation in the annotation sidecar
JSONs, so the visual result remains the same after the fix.

Usage
-----
    python3 scripts/migrate_rotations.py /path/to/library          # dry-run
    python3 scripts/migrate_rotations.py /path/to/library --apply  # write changes

Pages whose intrinsic rotation is 0 are unaffected.  Sidecars with no
``rotations`` entries are left alone.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from web.core import (  # noqa: E402
    SafeJSON,
    annotation_sidecar_path,
    normalize_path,
)


def page_intrinsic_rotations(pdf_path: str) -> dict[str, int]:
    """Return ``{page_index_str: intrinsic_rotation_degrees}`` for *pdf_path*.

    Only includes pages with a non-zero intrinsic rotation.
    """
    import pymupdf as fitz

    out: dict[str, int] = {}
    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            r = page.rotation % 360
            if r:
                out[str(i)] = r
    return out


def find_sidecars(library_path: str) -> list[tuple[str, str]]:
    """Return list of ``(pdf_path, sidecar_path)`` pairs under *library_path*."""
    pairs: list[tuple[str, str]] = []
    for root_dir, _subdirs, files in os.walk(library_path):
        for fname in files:
            if not fname.lower().endswith(".pdf"):
                continue
            pdf_path = os.path.join(root_dir, fname)
            sidecar = annotation_sidecar_path(pdf_path)
            if os.path.exists(sidecar):
                pairs.append((pdf_path, sidecar))
    return pairs


def migrate_sidecar(
    pdf_path: str, sidecar_path: str
) -> tuple[dict[str, tuple[int, int]], dict] | None:
    """Compute the migrated rotations for one sidecar.

    Returns ``(changes, new_data)`` where ``changes`` maps page index ->
    ``(old_rot, new_rot)`` for pages whose stored rotation changed, and
    ``new_data`` is the full sidecar dict with the migrated rotations
    substituted in.  Returns ``None`` if the sidecar is unreadable, has no
    rotations to migrate, or the PDF is missing.
    """
    if not os.path.exists(pdf_path):
        return None

    raw = SafeJSON.load(sidecar_path, default=None)
    if not isinstance(raw, dict):
        return None
    rotations = raw.get("rotations")
    if not isinstance(rotations, dict) or not rotations:
        return None

    try:
        intrinsic = page_intrinsic_rotations(pdf_path)
    except Exception as exc:  # PDF unreadable, corrupt, etc.
        print(f"  WARN: could not read {pdf_path}: {exc}", file=sys.stderr)
        return None

    if not intrinsic:
        return None  # nothing to subtract

    changes: dict[str, tuple[int, int]] = {}
    new_rotations: dict[str, int] = {}
    for pg, old_rot in rotations.items():
        if not isinstance(old_rot, int):
            new_rotations[pg] = old_rot  # leave malformed values alone
            continue
        adj = intrinsic.get(pg, 0)
        new_rot = (old_rot - adj) % 360
        if new_rot != old_rot % 360:
            changes[pg] = (old_rot % 360, new_rot)
        if new_rot:  # zero rotations get pruned on save
            new_rotations[pg] = new_rot

    if not changes:
        return None

    new_data = dict(raw)
    new_data["rotations"] = new_rotations
    return changes, new_data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate stored rotations after the viewer rotation fix."
    )
    parser.add_argument("library", help="Path to the music library directory")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes (default is dry-run)",
    )
    args = parser.parse_args()

    library_path = normalize_path(args.library)
    if not os.path.isdir(library_path):
        print(f"Error: not a directory: {library_path}", file=sys.stderr)
        sys.exit(1)

    sidecars = find_sidecars(library_path)
    if not sidecars:
        print("No annotation sidecars found.")
        return

    total_files = 0
    total_pages = 0
    pending: list[tuple[str, str, dict]] = []

    for pdf_path, sidecar_path in sidecars:
        result = migrate_sidecar(pdf_path, sidecar_path)
        if result is None:
            continue
        changes, new_data = result
        rel = os.path.relpath(pdf_path, library_path)
        print(f"\n{rel}")
        for pg in sorted(changes, key=lambda k: int(k) if k.isdigit() else 0):
            old_r, new_r = changes[pg]
            display_pg = int(pg) + 1 if pg.isdigit() else pg
            print(f"  page {display_pg}: {old_r}° -> {new_r}°")
        total_files += 1
        total_pages += len(changes)
        pending.append((pdf_path, sidecar_path, new_data))

    if not pending:
        print("No rotations need migration.")
        return

    print(
        f"\n{total_pages} page rotation(s) across {total_files} file(s) "
        "would be migrated."
    )

    if not args.apply:
        print("Dry run — no files were changed. Use --apply to write.")
        return

    written = 0
    for pdf_path, sidecar_path, new_data in pending:
        try:
            SafeJSON.save(sidecar_path, new_data)
            written += 1
        except Exception as exc:
            print(
                f"  ERROR writing {sidecar_path}: {exc}", file=sys.stderr
            )

    print(f"\nMigrated {written} sidecar file(s).")


if __name__ == "__main__":
    main()
