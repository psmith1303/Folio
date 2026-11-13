"""scripts/lighten_scans.py: the image steps (pure numpy/PIL) and the
done-list. The end-to-end test needs poppler and jbig2 and skips without
them (the deploy image has neither; it runs on okapi)."""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lighten_scans.py"
_spec = importlib.util.spec_from_file_location("lighten_scans", SCRIPT)
ls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ls)


# --- page size -------------------------------------------------------------

@pytest.mark.parametrize("w,h,expect", [
    (595.28, 841.89, (595.28, 841.89)),        # A4: untouched
    (612, 792, (612, 792)),                    # Letter: within 10%, left alone
    (841.89, 595.28, (841.89, 595.28)),        # A4 landscape
])
def test_fit_a4_leaves_near_a4_alone(w, h, expect):
    assert ls.fit_a4(w, h) == expect


def test_fit_a4_shrinks_a_big_page_keeping_its_shape():
    w, h = ls.fit_a4(1929, 2644)
    assert w <= 595.28 + 1e-6 and h <= 841.89 + 1e-6
    assert abs(w / h - 1929 / 2644) < 1e-9


def test_fit_a4_shrinks_a_landscape_page_landscape():
    w, h = ls.fit_a4(2000, 1400)
    assert w > h and w <= 841.89 + 1e-6 and h <= 595.28 + 1e-6


# --- threshold, specks, edges ----------------------------------------------

def test_otsu_splits_ink_from_paper():
    g = np.full((100, 100), 230, np.uint8)
    g[40:60] = 30
    t = ls.otsu(g)
    assert 30 < t <= 230


@pytest.mark.parametrize("r", [0, 1, 2, 3])
def test_box_sum_matches_brute_force(r):
    rng = np.random.default_rng(r)
    m = rng.random((23, 31)) < 0.4
    p = np.pad(m.astype(int), r)
    k = 2 * r + 1
    brute = np.array([[p[y:y + k, x:x + k].sum() for x in range(m.shape[1])]
                      for y in range(m.shape[0])])
    assert (ls.box_sum(m, r) == brute).all()


def test_remove_specks_keeps_dots_and_lines():
    b = np.zeros((60, 60), bool)
    b[5:7, 5:7] = True          # 2x2 speck
    b[20:23, 20:23] = True      # 3x3 speck
    b[0:2, 58:60] = True        # speck in a corner
    b[40:47, 40:47] = True      # 7x7 staccato dot
    b[30, 0:60] = True          # thin line touching both edges
    out = ls.remove_specks(b, 3)
    assert not out[5:7, 5:7].any() and not out[20:23, 20:23].any()
    assert not out[0:2, 58:60].any()
    assert out[40:47, 40:47].all() and out[30].all()


def test_clear_edges_clears_a_shadow_but_not_the_music():
    b = np.zeros((1000, 800), bool)
    b[:, 5:30] = True               # scanner shadow, a few px in from the edge
    b[100:102, :] = True            # staff line running into it
    b[500, 600:800] = True          # line touching the right edge only
    b[300:310, 400:410] = True      # a note in the middle
    ls.clear_edges(b)
    band = round(800 * 0.06)
    assert not b[:, 5:30].any()
    assert b[100:102, band:].all() and not b[100:102, :band].any()
    assert b[500, 600:800].all() and b[300:310, 400:410].all()


def test_clear_edges_leaves_artwork_that_bleeds_off_the_page():
    b = np.zeros((1000, 800), bool)
    b[:, :400] = True               # a cover's black panel from the left edge
    before = b.copy()
    ls.clear_edges(b)
    assert (b == before).all()


def test_skew_angle_of_a_blank_page_is_zero():
    # A tie between every angle used to return -5.25 degrees.
    assert ls.skew_angle(Image.new("L", (800, 1100), 255)) == 0.0


def test_skew_angle_finds_a_tilt():
    img = Image.new("L", (1200, 1600), 255)
    d = ImageDraw.Draw(img)
    for y in range(200, 1400, 40):
        d.line([(100, y), (1100, y)], fill=0, width=3)
    tilted = img.rotate(-1.5, resample=Image.BICUBIC, fillcolor=255)
    assert abs(ls.skew_angle(tilted) - 1.5) <= 0.15


# --- folders, options, done-list -------------------------------------------

def test_find_pdfs_skips_exclude_folders(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.pdf").write_bytes(b"1")
    (tmp_path / "hidden" / "deep").mkdir(parents=True)
    (tmp_path / "hidden" / ".exclude").touch()
    (tmp_path / "hidden" / "deep" / "two.pdf").write_bytes(b"2")
    (tmp_path / "a" / "notes.txt").write_text("x")
    assert ls.find_pdfs([tmp_path]) == [tmp_path / "a" / "one.pdf"]


def test_needs_lightening():
    a4 = [(595.28, 841.89)]
    assert ls.needs_lightening(a4, {1: 20.0}, 16.7)[0]
    assert not ls.needs_lightening(a4, {1: 5.0}, 16.7)[0]
    assert ls.needs_lightening([(1929, 2644)], {1: 5.0}, 16.7)[0]     # oversize
    assert not ls.needs_lightening([(1929, 2644)], {}, 16.7)[0]       # no images


@pytest.mark.parametrize("argv", [
    ["x.pdf", "--jobs", "0"],
    ["x.pdf", "--speck", "2"],
    ["x.pdf", "--speck", "-1"],
    ["x.pdf", "--threshold", "abc"],
    ["x.pdf", "--threshold", "150"],
])
def test_bad_options_are_refused(argv):
    with pytest.raises(SystemExit) as e:
        ls.parse_args(argv)
    assert e.value.code == 2


def test_settings_key_changes_with_the_options_that_change_a_copy():
    k = ls.settings_key(ls.parse_args(["x.pdf"]))
    assert k != ls.settings_key(ls.parse_args(["x.pdf", "--speck", "5"]))
    assert k != ls.settings_key(ls.parse_args(["x.pdf", "--no-deskew"]))
    assert k == ls.settings_key(ls.parse_args(["x.pdf", "--jobs", "1"]))


def test_load_done_last_line_wins(tmp_path):
    f = tmp_path / ".lighten-done.tsv"
    f.write_text("a.pdf\t1\t2\tS\tOK\nbad line\na.pdf\t3\t4\tS\tSKIP\n")
    assert ls.load_done(f) == {"a.pdf": ("3", "4", "S", "SKIP")}
    assert ls.load_done(tmp_path / "missing.tsv") == {}


# --- end to end (okapi: needs poppler and jbig2) ---------------------------

TOOLS = all(shutil.which(t) for t in ("pdftoppm", "pdfimages", "pdfinfo", "jbig2"))


@pytest.mark.skipif(not TOOLS, reason="needs poppler-utils and jbig2")
def test_end_to_end_makes_a_smaller_one_bit_copy_and_restarts(tmp_path):
    lib, out = tmp_path / "lib", tmp_path / "out"
    lib.mkdir()
    img = Image.new("L", (2480, 3508), 235)
    d = ImageDraw.Draw(img)
    for y in range(300, 3200, 25):
        d.line([(200, y), (2280, y)], fill=20, width=3)
    img.save(lib / "score.pdf", resolution=300, quality=95)
    cmd = [sys.executable, str(SCRIPT), "--root", str(lib), "--out", str(out),
           "--min-mp", "0", str(lib)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    assert r.stdout.startswith("OK\t"), r.stdout + r.stderr
    copy = out / "score.pdf"
    assert copy.stat().st_size < (lib / "score.pdf").stat().st_size
    lst = subprocess.run(["pdfimages", "-list", str(copy)], capture_output=True, text=True).stdout
    assert "jbig2" in lst
    # A second run finds it done.
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    assert "1 files already done" in r.stdout
