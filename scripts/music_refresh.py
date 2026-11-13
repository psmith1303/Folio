#!/usr/bin/env python3
"""music_refresh.py: fill Folio's library (Music) from Music-Master.

Music-Master holds the original scans and everything else Peter keeps with
them (audio, LilyPond, .exclude folders). Music, the folder Folio serves,
holds only what Folio shows: a light copy of each PDF outside a .exclude
folder (scripts/lighten_scans.py; small PDFs are copied as they are), plus
Folio's own json (annotation sidecars, setlists.json, _hash_index.json...).

Dry run by default: it reports what it would do and writes nothing.
--apply makes the changes. Each Master PDF falls in one class:

  NEW        not yet in Music: lighten (or copy) it
  ADOPT      Music already has the same file at that path (the first run):
             lighten it in place, or keep it
  CONFLICT   Music has a different file at that path: reported, left alone
  SAME       done before and unchanged: nothing to do (a new timestamp
             with the same content, as a sync may leave, isn't a change)
  CHANGED    the Master file (or the settings) changed: made again; its
             sidecar is kept
  MOVED      moved or renamed in Master: the Music copy and its sidecar are
             moved to match (Folio's next scan heals setlists and Recent)
  RENAMED    renamed in Folio (a tag edit renames the file): the Master
             file is renamed to match. The only write to Master.
  DELETED    gone from Master: reported; --prune deletes the Music copy
             (never its sidecar)
  GONE       its Music copy was deleted: reported, not made again;
             --restore makes it again

Other files in Music (audio, .exclude folders, ...) are deleted (PRUNE)
only when Master holds a byte-identical copy at the same path; otherwise
they are reported as MUSIC-ONLY and kept, as is a Folio PDF that Master
lacks. json files are never touched.

ANNOTATED lists the lightened PDFs whose sidecar has annotations: pages are
straightened, so check those marks still sit on the music.

State: MUSIC/.music-refresh.tsv, one line per file handled; an interrupted
run picks up where it stopped.

Usage:
  music_refresh.py [--master DIR] [--music DIR] [--apply] [--prune]
                   [--restore] [lighten_scans.py's options: --min-mp N,
                   --no-deskew, --jobs N, ...]
"""
import argparse
import filecmp
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import lighten_scans as ls  # noqa: E402
from web.core import EXCLUDE_MARKER, compute_content_hash  # noqa: E402

MANIFEST = ".music-refresh.tsv"
FIELDS = ("rel", "size", "mtime_ns", "master_hash", "music_hash", "action", "settings")


# --- reading the two trees -------------------------------------------------

def folio_pdfs(root):
    """{rel path (posix str): Path} of the PDFs Folio shows (outside .exclude
    folders)."""
    return {p.relative_to(root).as_posix(): p for p in ls.find_pdfs([root])}


def all_files(root):
    """{rel path: Path} of every file under root."""
    return {(Path(d) / f).relative_to(root).as_posix(): Path(d) / f
            for d, _, files in os.walk(root) for f in files}


def fast_hash(p):
    return compute_content_hash(str(p))


def sidecar(pdf):
    return pdf.with_suffix(".json")


def annotated(pdf):
    """True if pdf's sidecar holds any annotation (read-only: Folio's
    load_annotations may write the sidecar back)."""
    try:
        raw = json.loads(sidecar(pdf).read_text(encoding="utf-8"))
        pages = raw.get("pages", {}) if "version" in raw else raw
        return any(isinstance(v, list) and v for v in pages.values())
    except (OSError, ValueError, AttributeError, TypeError):   # odd json: not ours to judge
        return False


# --- the manifest ----------------------------------------------------------

def load_manifest(music):
    """{rel: row dict}; later lines win, a DROP line removes its row."""
    rows = {}
    path = music / MANIFEST
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        f = line.split("\t")
        if len(f) != len(FIELDS):
            continue
        row = dict(zip(FIELDS, f))
        if row["action"] == "DROP":
            rows.pop(row["rel"], None)
        else:
            rows[row["rel"]] = row
    return rows


class Manifest:
    def __init__(self, music, apply):
        self.music, self.apply = music, apply
        self.rows = load_manifest(music)

    def _write(self, row):
        if self.apply:
            with open(self.music / MANIFEST, "a", encoding="utf-8") as f:
                f.write("\t".join(row[k] for k in FIELDS) + "\n")

    def put(self, row):
        self.rows[row["rel"]] = row
        self._write(row)

    def drop(self, rel):
        self.rows.pop(rel, None)
        self._write({k: "" for k in FIELDS} | {"rel": rel, "action": "DROP"})

    def compact(self):
        """Rewrite the file with one line per row (it only ever grows)."""
        if not self.apply:
            return
        tmp = self.music / (MANIFEST + ".part")
        tmp.write_text("".join("\t".join(r[k] for k in FIELDS) + "\n"
                               for _, r in sorted(self.rows.items())), encoding="utf-8")
        os.replace(tmp, self.music / MANIFEST)


# --- actions ---------------------------------------------------------------

def copy_file(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    shutil.copy2(src, part)
    os.replace(part, dst)


def move(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)


def taken(dst, src):
    """dst holds a file other than src (os.rename would overwrite it). The
    same file is fine: a case-only rename on a case-insensitive drive."""
    return dst.exists() and not os.path.samefile(dst, src)


def make_copy(src, dst, la, same_already):
    """Lighten src to dst, or copy it as it is. Returns (action, detail).
    same_already: dst already holds src's bytes (ADOPT), so a plain copy
    is not needed."""
    why = ""
    try:
        go, heaviest, _, boxes, mp, ppi = ls.assess(src, la.jobs, la.min_mp)
        if go:
            status, detail = ls.lighten(src, dst, la, boxes, mp, ppi)
            if status == "OK":
                return "LIGHT", detail
            why = f"lighten {status}: {detail}; "
        else:
            why = f"{heaviest:.1f} MP; "
    except Exception as e:      # a PDF poppler can't read is still copied
        why = f"lighten failed ({type(e).__name__}: {str(e)[:120]}); "
    if same_already:
        return "COPY", why + "kept as it is"
    copy_file(src, dst)
    return "COPY", why + "copied as it is"


def would_lighten(src, la):
    try:
        go, heaviest, oversize, *_ = ls.assess(src, la.jobs, la.min_mp)
    except Exception as e:
        return f"would copy (unreadable: {type(e).__name__})"
    if go:
        return f"would lighten ({ls.describe(heaviest, oversize)})"
    return f"would copy ({heaviest:.1f} MP)"


# --- the run ---------------------------------------------------------------

def check_dirs(master, music):
    """Refuse a run that could write to the wrong tree."""
    if not master.is_dir() or not music.is_dir():
        return f"not a folder: {master if not master.is_dir() else music}"
    m, u = master.resolve(), music.resolve()
    if m == u or m in u.parents or u in m.parents:
        return "--master and --music must be separate folders"
    if not (music / "setlists.json").exists():
        return f"{music} has no setlists.json: is it Folio's library?"
    for d, _, files in os.walk(master):
        if any(f.lower().endswith(".json") for f in files):
            return (f"{master} holds json files ({d}): Master keeps originals only;"
                    " are --master and --music the right way round?")
    return None


def refresh(master, music, la, apply=False, prune=False, restore=False, out=print):
    """Bring music up to date with master. Returns {class: count}."""
    counts = {}
    annotated_list = []
    emptied = set()                     # Music folders a delete or move may have emptied

    def report(cls, detail, rel):
        counts[cls] = counts.get(cls, 0) + 1
        out(f"{cls}\t{detail}\t{rel}")

    man = Manifest(music, apply)
    m_pdfs = folio_pdfs(master)
    u_pdfs = folio_pdfs(music)
    settings = ls.settings_key(la)

    def stamp(p):
        st = p.stat()
        return str(st.st_size), str(st.st_mtime_ns)

    def record(rel, src, dst, action):
        size, mtime = stamp(src)
        man.put({"rel": rel, "size": size, "mtime_ns": mtime,
                 "master_hash": fast_hash(src), "music_hash": fast_hash(dst),
                 "action": action, "settings": settings})

    def rekey(old, new):
        """The manifest row for old now describes new (written with --apply)."""
        row = dict(man.rows[old], rel=new)
        man.drop(old)
        man.put(row)

    # Renamed in Folio: a Music PDF unknown to the manifest whose bytes are
    # a recorded copy that has vanished from its own path. Without --apply
    # the maps keep pointing at the files where they still are.
    lost_music = {r for r in man.rows if r not in u_pdfs}
    unknown = [r for r in u_pdfs if r not in man.rows and r not in m_pdfs] if lost_music else []
    for rel in sorted(unknown):
        h = fast_hash(u_pdfs[rel])
        old = [o for o in lost_music if h and man.rows[o]["music_hash"] == h]
        if len(old) == 1 and old[0] in m_pdfs:
            o = old[0]
            if taken(master / rel, m_pdfs[o]):
                report("CONFLICT", f"renamed in Folio from {o}, but Master has another"
                       " file at the new name; left alone", rel)
                continue
            report("RENAMED", f"in Folio from {o}; Master follows", rel)
            if apply:
                move(m_pdfs[o], master / rel)
            m_pdfs[rel] = master / rel if apply else m_pdfs[o]
            del m_pdfs[o]
            lost_music.discard(o)
            rekey(o, rel)

    # Moved in Master: a Master PDF unknown to the manifest whose bytes match
    # a recorded original that has vanished from its own path.
    lost_master = {r for r in man.rows if r not in m_pdfs}
    moved_from = set()
    unknown = [r for r in m_pdfs if r not in man.rows] if lost_master else []
    for rel in sorted(unknown):
        h = fast_hash(m_pdfs[rel])
        old = [o for o in lost_master if h and man.rows[o]["master_hash"] == h]
        if len(old) == 1 and old[0] in u_pdfs and rel not in u_pdfs:
            o = old[0]
            if taken(music / rel, u_pdfs[o]):
                report("CONFLICT", f"moved in Master from {o}, but Music has another"
                       " file at the new path; left alone", rel)
                continue
            report("MOVED", f"in Master from {o}; Music copy follows", rel)
            if apply:
                emptied.add(u_pdfs[o].parent)
                move(u_pdfs[o], music / rel)
                if sidecar(u_pdfs[o]).exists() and not sidecar(music / rel).exists():
                    move(sidecar(u_pdfs[o]), sidecar(music / rel))
            u_pdfs[rel] = music / rel if apply else u_pdfs[o]
            del u_pdfs[o]
            moved_from.add(o)
            lost_master.discard(o)
            rekey(o, rel)

    # Each Master PDF.
    for rel in sorted(m_pdfs):
        src, dst = m_pdfs[rel], u_pdfs.get(rel, music / rel)
        row = man.rows.get(rel)
        if row is None:
            if not dst.exists():
                cls = "NEW"
            elif filecmp.cmp(dst, src, shallow=False):
                cls = "ADOPT"
            else:
                report("CONFLICT", "Music has a different file here; left alone", rel)
                continue
        elif not dst.exists():
            if not restore:
                report("GONE", "deleted from Music; --restore makes it again", rel)
                continue
            cls = "NEW"
        elif row["settings"] == settings and (row["size"], row["mtime_ns"]) == stamp(src):
            counts["SAME"] = counts.get("SAME", 0) + 1
            continue
        elif row["settings"] == settings and row["master_hash"] and fast_hash(src) == row["master_hash"]:
            # Only the timestamp moved (a mirror sync can do that): record the
            # new one rather than redo a slow lighten.
            if apply:
                size, mtime = stamp(src)
                man.put(dict(row, size=size, mtime_ns=mtime))
            counts["SAME"] = counts.get("SAME", 0) + 1
            continue
        else:
            cls = "CHANGED"
        if not apply:
            report(cls, would_lighten(src, la), rel)
            continue
        t = time.time()
        action, detail = make_copy(src, dst, la, cls == "ADOPT")
        record(rel, src, dst, action)
        report(cls, f"{action}: {detail} ({time.time() - t:.0f} s)", rel)
        if action == "LIGHT" and annotated(dst):
            annotated_list.append(rel)

    # Recorded files gone from Master.
    for rel in sorted(r for r in man.rows if r not in m_pdfs):
        dst = music / rel
        if not dst.exists():            # gone from both: nothing left to do
            if apply:
                man.drop(rel)
        elif prune:
            report("DELETED", "gone from Master; Music copy deleted"
                   + ("; its sidecar is kept" if sidecar(dst).exists() else ""), rel)
            if apply:
                dst.unlink()
                emptied.add(dst.parent)
                man.drop(rel)
        else:
            report("DELETED", "gone from Master; --prune deletes the Music copy", rel)

    # Everything else in Music.
    # Classify every file before deleting any: pruning a folder's .exclude
    # marker would make the PDFs beside it look like Folio's.
    shown = folio_pdfs(music)
    extras = []
    for rel, p in sorted(all_files(music).items()):
        if rel in shown:
            if rel not in man.rows and rel not in m_pdfs and rel not in moved_from:
                report("MUSIC-ONLY", "PDF not in Master; copy it there to keep it", rel)
        elif rel != MANIFEST and not rel.lower().endswith(".json"):
            extras.append((rel, p))
    same = {rel for rel, p in extras
            if (master / rel).is_file() and filecmp.cmp(master / rel, p, shallow=False)}
    # A .exclude marker stays while a kept file sits under its folder:
    # without it Folio would show that file.
    kept = [Path(rel).parent for rel, _ in extras if rel not in same]
    for rel, p in extras:
        d = Path(rel).parent
        if rel in same and not (Path(rel).name == EXCLUDE_MARKER
                                and any(k == d or d in k.parents for k in kept)):
            report("PRUNE", "not shown by Folio; identical copy in Master", rel)
            if apply:
                p.unlink()
                emptied.add(p.parent)
        elif rel in same:
            report("MUSIC-ONLY", "kept: it hides files kept beside it", rel)
        else:
            report("MUSIC-ONLY", "not shown by Folio, and no identical copy in Master: kept", rel)
    if apply:
        # Only folders this run emptied: an empty folder made on purpose stays.
        for d in sorted(emptied, key=lambda d: len(d.parts), reverse=True):
            while d != music and music in d.parents and d.is_dir() and not os.listdir(d):
                d.rmdir()
                d = d.parent
        man.compact()

    for rel in annotated_list:
        out(f"ANNOTATED\tlightened and straightened: check its marks\t{rel}")
    out("TOTAL\t" + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 parents=[ls.options_parser()])
    ap.add_argument("--master", type=Path, default=Path("/mnt/z/psDATA/Resources/Music-Master"))
    ap.add_argument("--music", type=Path, default=Path("/mnt/z/psDATA/Resources/Music"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--restore", action="store_true")
    a = ap.parse_args(argv)
    ls.check_args(ap, a)
    err = check_dirs(a.master, a.music)
    if err:
        ap.error(err)
    if a.apply and a.mode == "bw" and not shutil.which("jbig2"):
        sys.exit("jbig2 (jbig2enc) is not installed")
    refresh(a.master, a.music, a, apply=a.apply, prune=a.prune, restore=a.restore,
            out=lambda s: print(s, flush=True))


if __name__ == "__main__":
    main()
