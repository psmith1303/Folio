"""scripts/music_refresh.py: Music-Master -> Music. Lightening is stubbed
(lighten_scans's PDF tools are tested in test_lighten_scans.py): a "heavy"
fake PDF is "lightened" to b"LIGHT:" + its bytes."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "music_refresh.py"
_spec = importlib.util.spec_from_file_location("music_refresh", SCRIPT)
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)

A4 = [(595.28, 841.89)]


@pytest.fixture(autouse=True)
def stub_lighten(monkeypatch):
    def image_stats(src, pages, jobs):
        return ({1: 40.0 if b"heavy" in Path(src).read_bytes() else 1.0}, {})

    def lighten(src, dst, a, boxes, mp, ppi):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"LIGHT:" + Path(src).read_bytes())
        return "OK", "stub"

    monkeypatch.setattr(mr.ls, "page_boxes", lambda src: A4)
    monkeypatch.setattr(mr.ls, "image_stats", image_stats)
    monkeypatch.setattr(mr.ls, "lighten", lighten)


@pytest.fixture
def la():
    return mr.ls.options_parser().parse_args(["--jobs", "1"])


@pytest.fixture
def trees(tmp_path):
    """A Music library (as today: originals + json) and its Master copy."""
    master, music = tmp_path / "Master", tmp_path / "Music"
    files = {
        "Tech/heavy.pdf": b"%PDF heavy one",
        "Tech/small.pdf": b"%PDF small two",
        "Audio/.exclude": b"",
        "Audio/track.mp3": b"ID3 three",
        "Audio/hidden.pdf": b"%PDF heavy hidden",
        "Tech/notes.docx": b"PK four",
    }
    for rel, data in files.items():
        for root in (master, music):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)
    (music / "setlists.json").write_text("{}")
    (music / "Tech" / "heavy.json").write_text(
        json.dumps({"version": 2, "rotations": {}, "pages": {"1": [{"type": "ink"}]}}))
    return master, music


def run(master, music, la, **kw):
    lines = []
    counts = mr.refresh(master, music, la, out=lines.append, **kw)
    return counts, lines


def line(lines, cls, rel):
    return any(ln.startswith(cls + "\t") and ln.endswith("\t" + rel) for ln in lines)


def snapshot(*roots):
    return {(str(r), p.relative_to(r).as_posix(), p.read_bytes())
            for r in roots for p in r.rglob("*") if p.is_file()}


# --- the first run ----------------------------------------------------------

def test_first_run_lightens_heavy_keeps_small_and_prunes_the_rest(trees, la):
    master, music = trees
    counts, lines = run(master, music, la, apply=True)
    assert (music / "Tech/heavy.pdf").read_bytes() == b"LIGHT:%PDF heavy one"
    assert (music / "Tech/small.pdf").read_bytes() == b"%PDF small two"
    assert counts["ADOPT"] == 2
    # Not shown by Folio, identical in Master: gone from Music, Master untouched.
    for rel in ("Audio/track.mp3", "Audio/hidden.pdf", "Audio/.exclude", "Tech/notes.docx"):
        assert line(lines, "PRUNE", rel)
        assert not (music / rel).exists() and (master / rel).exists()
    assert not (music / "Audio").exists()                 # empty folder removed
    # json is never touched.
    assert (music / "setlists.json").exists() and (music / "Tech/heavy.json").exists()
    assert line(lines, "ANNOTATED", "Tech/heavy.pdf")
    assert (master / "Tech/heavy.pdf").read_bytes() == b"%PDF heavy one"


def test_dry_run_writes_nothing(trees, la):
    master, music = trees
    before = snapshot(master, music)
    counts, lines = run(master, music, la)
    assert snapshot(master, music) == before
    assert line(lines, "ADOPT", "Tech/heavy.pdf") and "would lighten" in lines[0] + lines[1]
    assert counts["PRUNE"] == 4


def test_second_run_finds_nothing_to_do(trees, la):
    master, music = trees
    run(master, music, la, apply=True)
    before = snapshot(master, music)
    counts, _ = run(master, music, la, apply=True)
    assert counts == {"SAME": 2}
    assert snapshot(master, music) == before


def test_restart_after_a_crash_does_the_rest(trees, la, monkeypatch):
    master, music = trees
    (master / "Tech/z-heavy.pdf").write_bytes(b"%PDF heavy last")
    real = mr.ls.lighten
    def crash_on_last(src, dst, *a):
        if src.name == "z-heavy.pdf":
            raise KeyboardInterrupt
        return real(src, dst, *a)
    monkeypatch.setattr(mr.ls, "lighten", crash_on_last)
    with pytest.raises(KeyboardInterrupt):
        run(master, music, la, apply=True)
    monkeypatch.setattr(mr.ls, "lighten", real)
    counts, lines = run(master, music, la, apply=True)
    assert counts["SAME"] == 2 and counts["NEW"] == 1
    assert (music / "Tech/z-heavy.pdf").read_bytes() == b"LIGHT:%PDF heavy last"


# --- later runs -------------------------------------------------------------

def test_new_and_changed_files(trees, la):
    master, music = trees
    run(master, music, la, apply=True)
    (master / "Tech/new.pdf").write_bytes(b"%PDF heavy new")
    (master / "Tech/small.pdf").write_bytes(b"%PDF small two, corrected")
    counts, lines = run(master, music, la, apply=True)
    assert line(lines, "NEW", "Tech/new.pdf") and line(lines, "CHANGED", "Tech/small.pdf")
    assert (music / "Tech/new.pdf").read_bytes() == b"LIGHT:%PDF heavy new"
    assert (music / "Tech/small.pdf").read_bytes() == b"%PDF small two, corrected"


def test_a_new_timestamp_alone_is_not_a_change(trees, la):
    master, music = trees
    run(master, music, la, apply=True)
    light = (music / "Tech/heavy.pdf").read_bytes()
    st = (master / "Tech/heavy.pdf").stat()
    os.utime(master / "Tech/heavy.pdf", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert run(master, music, la, apply=True)[0] == {"SAME": 2}
    assert (music / "Tech/heavy.pdf").read_bytes() == light
    row = mr.Manifest(music, False).rows["Tech/heavy.pdf"]
    assert row["mtime_ns"] == str(st.st_mtime_ns + 10**9)   # recorded: no rehash next time


def test_adopt_needs_every_byte_to_match(trees, la):
    # Same size, same first and last 4 KB (all Folio's quick hash reads):
    # only a full compare tells them apart.
    master, music = trees
    body = b"%PDF heavy " + b"x" * 5000 + b"A" + b"y" * 5000
    other = body.replace(b"A", b"B")
    (master / "Tech/big.pdf").write_bytes(body)
    (music / "Tech/big.pdf").write_bytes(other)
    assert mr.fast_hash(master / "Tech/big.pdf") == mr.fast_hash(music / "Tech/big.pdf")
    _, lines = run(master, music, la, apply=True)
    assert line(lines, "CONFLICT", "Tech/big.pdf")
    assert (music / "Tech/big.pdf").read_bytes() == other


def test_conflict_is_left_alone(trees, la):
    master, music = trees
    (music / "Tech/small.pdf").write_bytes(b"%PDF someone else's")
    counts, lines = run(master, music, la, apply=True)
    assert line(lines, "CONFLICT", "Tech/small.pdf")
    assert (music / "Tech/small.pdf").read_bytes() == b"%PDF someone else's"


def test_renamed_in_folio_renames_master(trees, la):
    master, music = trees
    run(master, music, la, apply=True)
    # Folio's tag edit: PDF and sidecar renamed together.
    os.rename(music / "Tech/heavy.pdf", music / "Tech/heavy -- tagged.pdf")
    os.rename(music / "Tech/heavy.json", music / "Tech/heavy -- tagged.json")
    counts, lines = run(master, music, la, apply=True)
    assert line(lines, "RENAMED", "Tech/heavy -- tagged.pdf")
    assert (master / "Tech/heavy -- tagged.pdf").read_bytes() == b"%PDF heavy one"
    assert not (master / "Tech/heavy.pdf").exists()
    assert not (music / "Tech/heavy.pdf").exists()        # not brought back
    assert run(master, music, la, apply=True)[0] == {"SAME": 2}


def test_moved_in_master_moves_the_copy_and_its_sidecar(trees, la):
    master, music = trees
    run(master, music, la, apply=True)
    (master / "Daily").mkdir()
    os.rename(master / "Tech/heavy.pdf", master / "Daily/heavy.pdf")
    counts, lines = run(master, music, la, apply=True)
    assert line(lines, "MOVED", "Daily/heavy.pdf")
    assert (music / "Daily/heavy.pdf").read_bytes() == b"LIGHT:%PDF heavy one"
    assert (music / "Daily/heavy.json").exists() and not (music / "Tech/heavy.json").exists()
    assert not (music / "Tech/heavy.pdf").exists()
    assert run(master, music, la, apply=True)[0] == {"SAME": 2}


def test_moves_and_renames_in_a_dry_run_write_nothing(trees, la):
    master, music = trees
    run(master, music, la, apply=True)
    os.rename(music / "Tech/heavy.pdf", music / "Tech/heavy2.pdf")
    (master / "Daily").mkdir()
    os.rename(master / "Tech/small.pdf", master / "Daily/small.pdf")
    before = snapshot(master, music)
    counts, lines = run(master, music, la)
    assert snapshot(master, music) == before
    assert counts == {"RENAMED": 1, "MOVED": 1, "SAME": 2}


def test_deleted_from_master_needs_prune(trees, la):
    master, music = trees
    run(master, music, la, apply=True)
    (master / "Tech/heavy.pdf").unlink()
    _, lines = run(master, music, la, apply=True)
    assert line(lines, "DELETED", "Tech/heavy.pdf") and (music / "Tech/heavy.pdf").exists()
    run(master, music, la, apply=True, prune=True)
    assert not (music / "Tech/heavy.pdf").exists()
    assert (music / "Tech/heavy.json").exists()           # sidecar kept


def test_deleted_from_both_is_forgotten(trees, la):
    master, music = trees
    run(master, music, la, apply=True)
    (master / "Tech/heavy.pdf").unlink()
    (music / "Tech/heavy.pdf").unlink()
    counts, _ = run(master, music, la, apply=True, prune=True)
    assert "DELETED" not in counts
    assert "Tech/heavy.pdf" not in mr.Manifest(music, False).rows


def test_deleted_from_music_is_not_brought_back_without_restore(trees, la):
    master, music = trees
    run(master, music, la, apply=True)
    (music / "Tech/small.pdf").unlink()
    _, lines = run(master, music, la, apply=True)
    assert line(lines, "GONE", "Tech/small.pdf") and not (music / "Tech/small.pdf").exists()
    run(master, music, la, apply=True, restore=True)
    assert (music / "Tech/small.pdf").read_bytes() == b"%PDF small two"


def test_prune_keeps_a_file_whose_master_copy_differs(trees, la):
    master, music = trees
    (master / "Audio/track.mp3").write_bytes(b"ID3 threE")  # same size, one byte off
    _, lines = run(master, music, la, apply=True)
    assert line(lines, "MUSIC-ONLY", "Audio/track.mp3")
    assert (music / "Audio/track.mp3").read_bytes() == b"ID3 three"


def test_a_pdf_only_in_music_is_reported_and_kept(trees, la):
    master, music = trees
    (music / "Tech/only-here.pdf").write_bytes(b"%PDF only here")
    _, lines = run(master, music, la, apply=True)
    assert line(lines, "MUSIC-ONLY", "Tech/only-here.pdf")
    assert (music / "Tech/only-here.pdf").exists()


def test_unreadable_pdf_is_copied_as_it_is(trees, la, monkeypatch):
    master, music = trees
    def broken(src):
        raise RuntimeError("bad xref")
    monkeypatch.setattr(mr.ls, "page_boxes", broken)
    (master / "Tech/new.pdf").write_bytes(b"%PDF heavy broken")
    _, lines = run(master, music, la, apply=True)
    assert (music / "Tech/new.pdf").read_bytes() == b"%PDF heavy broken"
    assert any("lighten failed" in ln and ln.endswith("Tech/new.pdf") for ln in lines)


def test_an_exclude_marker_stays_while_a_kept_file_needs_it(trees, la):
    master, music = trees
    (music / "Audio/only-here.pdf").write_bytes(b"%PDF only in music")
    _, lines = run(master, music, la, apply=True)
    assert line(lines, "MUSIC-ONLY", "Audio/.exclude")
    assert (music / "Audio/.exclude").exists() and (music / "Audio/only-here.pdf").exists()
    assert line(lines, "PRUNE", "Audio/track.mp3")


def test_only_folders_the_run_emptied_are_removed(trees, la):
    master, music = trees
    (music / "New tag folder").mkdir()
    run(master, music, la, apply=True)
    assert (music / "New tag folder").is_dir() and not (music / "Audio").exists()


def test_a_rename_never_overwrites_another_master_file(trees, la, monkeypatch):
    master, music = trees
    run(master, music, la, apply=True)
    os.rename(music / "Tech/heavy.pdf", music / "Tech/small2.pdf")
    (master / "Tech/small2.pdf").write_bytes(b"%PDF another original")
    # The walk misses Master's file at the new name, as it would a case
    # variant on the drvfs drive (Tech/Small2.PDF): the rename must not
    # land on it.
    real = mr.folio_pdfs
    monkeypatch.setattr(mr, "folio_pdfs", lambda root: {
        k: v for k, v in real(root).items() if not (root == master and k == "Tech/small2.pdf")})
    _, lines = run(master, music, la, apply=True)
    assert line(lines, "CONFLICT", "Tech/small2.pdf")
    assert (master / "Tech/small2.pdf").read_bytes() == b"%PDF another original"
    assert (master / "Tech/heavy.pdf").read_bytes() == b"%PDF heavy one"


def test_odd_sidecar_json_is_not_annotated(tmp_path):
    pdf = tmp_path / "a.pdf"
    for text in ("[1, 2]", "5", '{"version": 2, "pages": null}'):
        (tmp_path / "a.json").write_text(text)
        assert mr.annotated(pdf) is False


# --- the guard --------------------------------------------------------------

def test_swapped_folders_are_refused(trees):
    master, music = trees
    assert mr.check_dirs(master, music) is None
    assert mr.check_dirs(music, master) is not None
    assert mr.check_dirs(music, music) is not None
    (master / "stray.json").write_text("{}")
    assert "json" in mr.check_dirs(master, music)


def test_main_takes_lighten_options_and_needs_jbig2_only_for_bw(trees, monkeypatch, capsys):
    master, music = trees
    dirs = ["--master", str(master), "--music", str(music), "--jobs", "1"]
    monkeypatch.setattr(mr.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as e:
        mr.main(dirs + ["--apply"])
    assert "jbig2" in str(e.value.code)
    with pytest.raises(SystemExit) as e:
        mr.main(dirs + ["--speck", "2"])            # lighten's own checks apply
    assert e.value.code == 2
    mr.main(dirs + ["--apply", "--mode", "gray"])   # gray never calls jbig2
    assert (music / "Tech/heavy.pdf").read_bytes() == b"LIGHT:%PDF heavy one"


def test_main_refuses_swapped_folders_before_touching_anything(trees, capsys):
    master, music = trees
    before = snapshot(master, music)
    with pytest.raises(SystemExit) as e:
        mr.main(["--master", str(music), "--music", str(master), "--apply"])
    assert e.value.code == 2
    assert snapshot(master, music) == before
