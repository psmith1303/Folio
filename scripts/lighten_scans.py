#!/usr/bin/env python3
"""lighten_scans.py: make light copies of heavy scanned PDFs for Folio.

scripts/music_refresh.py uses it to fill the library from Music-Master
(README.md, "Music-Master and the light library"). Each page is:
  - shrunk to fit A4 (aspect ratio kept) if fitting needs more than a 10%
    shrink;
    Letter and other near-A4 sizes are left alone;
  - drawn in grey at 300 dpi (at most 2480 x 3508 px); in black-and-white,
    a low-resolution scan is drawn up to 300 dpi before the cut-off, which
    keeps curves and thin lines smooth;
  - straightened (deskew), then cropped back to its size;
  - black-and-white (default): cut off at the Otsu threshold (picked per
    page), dark strips along the edges and stray specks removed,
    compressed with JBIG2 (lossless, generic mode: no symbol
    substitution);
  - grey (--mode gray, for scans with photos): JPEG, quality 75; a scan
    is never drawn above its own resolution.

Annotations are stored as fractions of the page, so they stay put through
the shrink, but straightening moves the music under them: fix annotated
pages by hand, or use --no-deskew. Each copy is checked against its
original (page count, every page's shape and its expected size, smaller
file); a copy that fails is not kept. Originals are never written to.

Usage:
  lighten_scans.py [options] PDF-or-folder ...

  Folders are searched for PDFs, skipping any with a .exclude file, as
  Folio does.

  --out DIR        output folder (default /mnt/c/Temp/Output); files keep
                   their path relative to --root, e.g. Output/Technique/x.pdf
  --root DIR       library root (default /mnt/z/psDATA/Resources/Music)
  --min-mp N       only files whose heaviest page has N+ megapixels of
                   images, or a page bigger than A4 (default 16.7);
                   0 = every file with images (a file with no images,
                   such as a born-digital PDF, is always left alone)
  --mode bw|gray   black-and-white JBIG2 (default) or greyscale JPEG
  --threshold P    black/white cut-off: "otsu" (default) or a percent
  --speck N        remove black specks that fit in an N x N pixel square (N odd)
                   (default 3: 0.25 mm at 300 dpi; a staccato dot is ~7 px
                   across); 0 = keep them
  --no-deskew      don't straighten pages
  --keep-edges     keep dark strips along page edges (black-and-white only;
                   by default a strip running most of an edge's length,
                   such as a scanner's shadow, is cleared to 6% in;
                   artwork bleeding off a cover is left alone)
  --jobs N         pages processed at once (default: number of CPUs)
  --keep-larger    keep a copy even if it isn't smaller
  --dry-run        list what would be done
  --redo           ignore the done-list (below) and redo every file

Restarting: each finished file (OK, SKIP or LIGHT) is recorded in
OUT/.lighten-done.tsv with its size, modified time and the settings used.
A rerun skips files recorded as unchanged, so an interrupted batch picks
up where it stopped, and a later run over the whole library only works on
new or changed scores. FAIL is not recorded: it is retried.
"""
import argparse, os, re, shutil, subprocess, sys, tempfile, time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from web.core import is_excluded_dir  # noqa: E402

Image.MAX_IMAGE_PIXELS = None

DPI = 300
A4 = (595.28, 841.89)          # points, portrait


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, check=True,
                          stdin=subprocess.DEVNULL, **kw).stdout


def page_geometry(pdf):
    """({page: (w_pt, h_pt) CropBox}, {page: /Rotate}), as pdfinfo reports them."""
    out = run(["pdfinfo", "-box", "-f", "1", "-l", "1000000", str(pdf)]).decode(errors="replace")
    crop, rot = {}, {}
    for line in out.splitlines():
        m = re.match(r"Page\s+(\d+)\s+CropBox:\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)", line)
        if m:
            n, x0, y0, x1, y1 = int(m[1]), *map(float, m.groups()[1:])
            crop[n] = (abs(x1 - x0), abs(y1 - y0))
        m = re.match(r"Page\s+(\d+)\s+rot:\s+(\d+)", line)
        if m:
            rot[int(m[1])] = int(m[2])
    return crop, rot


def page_boxes(pdf):
    """[(w_pt, h_pt)] per page as displayed: CropBox with /Rotate applied."""
    crop, rot = page_geometry(pdf)
    return [crop[n] if rot.get(n, 0) % 180 == 0 else crop[n][::-1] for n in sorted(crop)]


def image_stats(pdf, pages, jobs):
    """({page: megapixels of images}, {page: highest image ppi}).

    pdfimages -list decodes every image (~1 s a page on a 38 MP scan), so
    the pages are listed in chunks, in parallel."""
    step = 8
    with ThreadPoolExecutor(jobs) as ex:
        outs = ex.map(lambda f: run(["pdfimages", "-list", "-f", str(f), "-l",
                                     str(min(f + step - 1, pages)), str(pdf)]),
                      range(1, pages + 1, step))
        lines = [l for out in outs for l in out.decode(errors="replace").splitlines()[2:]]
    mp, ppi = defaultdict(float), defaultdict(int)
    for line in lines:
        f = line.split()
        if len(f) >= 14 and f[2] == "image":
            p = int(f[0])
            mp[p] += int(f[3]) * int(f[4]) / 1e6
            if f[12].isdigit():
                ppi[p] = max(ppi[p], int(f[12]))
    return mp, ppi


def fit_a4(w, h):
    """The page's size shrunk (never grown) to fit A4, either way round."""
    short, long_ = (A4 if w <= h else A4[::-1])
    s = min(1.0, short / w, long_ / h)
    if s > 0.9:             # Letter, or a scanner's 595.3 pt: left alone
        return w, h
    return w * s, h * s


def do_page(src, n, w_pt, h_pt, rot, native_ppi, tmp, a):
    """Render page n of src (turned rot degrees by /Rotate) to w_pt x h_pt,
    as displayed; return (file, px w, px h, filter)."""
    dpi = DPI
    if a.mode == "gray" and native_ppi:
        dpi = min(dpi, native_ppi)          # grey: no point inventing pixels
    wpx, hpx = round(w_pt * dpi / 72), round(h_pt * dpi / 72)
    base = tmp / f"p{n:05d}"
    # Straight to the final pixel size, in grey; PGM is uncompressed, so
    # nothing is spent encoding a file that is read once. pdftoppm sizes the
    # page before turning it, so a quarter-turned page takes the sizes swapped.
    sx, sy = (hpx, wpx) if rot % 180 == 90 else (wpx, hpx)
    run(["pdftoppm", "-f", str(n), "-l", str(n), "-gray", "-singlefile",
         "-scale-to-x", str(sx), "-scale-to-y", str(sy), str(src), str(base)])
    img = Image.open(f"{base}.pgm"); img.load()
    os.remove(f"{base}.pgm")
    if img.size != (wpx, hpx):
        # Written as wpx x hpx, it would come out as streaks.
        raise RuntimeError(f"page {n} rendered {img.size[0]}x{img.size[1]},"
                           f" not {wpx}x{hpx}")
    if a.deskew:
        angle = skew_angle(img)
        if abs(angle) >= 0.05:
            # Same size, white fill: the page keeps its size.
            img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=255)
    if a.mode == "gray":
        out = f"{base}.jpg"
        img.save(out, quality=75)
        return out, wpx, hpx, "DCTDecode", 8
    g = np.asarray(img)
    black = g < (otsu(g) if a.threshold == "otsu" else 255 * float(a.threshold) / 100)
    if a.edges:
        clear_edges(black)
    if a.speck:
        black = remove_specks(black, a.speck)
    Image.fromarray(~black).save(f"{base}.pbm")    # bool array: mode "1"
    # -p: PDF-ready data; no -s, so lossless generic coding.
    Path(f"{base}.jb2").write_bytes(run(["jbig2", "-p", f"{base}.pbm"]))
    os.remove(f"{base}.pbm")
    return f"{base}.jb2", wpx, hpx, "JBIG2Decode", 1


def otsu(g):
    """The grey level that best splits the page into ink and paper."""
    hist = np.bincount(g.ravel(), minlength=256).astype(float)
    w0 = np.cumsum(hist); w1 = w0[-1] - w0
    m = np.cumsum(hist * np.arange(256))
    mu0 = m / np.maximum(w0, 1); mu1 = (m[-1] - m) / np.maximum(w1, 1)
    return int(np.argmax(w0 * w1 * (mu0 - mu1) ** 2)) + 1


def skew_angle(img, limit=5.0):
    """Tilt in degrees, from a quarter-size copy: the angle whose rotation
    gives the sharpest row profile (staff lines and text lines line up)."""
    g = np.asarray(img.reduce(4))
    small = Image.fromarray((g < otsu(g)).astype(np.uint8) * 255)
    def score(a):
        rows = np.asarray(small.rotate(a, resample=Image.BILINEAR)).sum(axis=1, dtype=np.float64)
        return float(np.var(rows))
    flat = score(0.0)
    best = max(np.arange(-limit, limit + 0.01, 0.25), key=score)
    if score(best) <= flat * 1.001:     # blank page or artwork: no clear tilt
        return 0.0
    fine = max(np.arange(best - 0.25, best + 0.26, 0.05), key=score)
    return float(np.clip(fine, -limit, limit))


def box_sum(m, r):
    """Count of True in the (2r+1)-square around each pixel. Shifted adds,
    rows then columns: ~15x faster than a cumsum for these small squares."""
    k, (h, w) = 2 * r + 1, m.shape
    p = np.pad(m.astype(np.uint16), r)
    rows = sum(p[:, i:i + w] for i in range(k))
    return sum(rows[i:i + h] for i in range(k))


def clear_edges(black, band=0.06, seed=0.02, cover=0.5, bleed=0.1):
    """Whiten dark strips along the page edges (scanner shadow, book gutter).

    A side has a strip when ink lies within `seed` of that edge on at least
    `cover` of its length; music near an edge only touches it in places.
    If ink also covers `bleed` of the line just past `band`, it is
    artwork bleeding off the page (a cover), not a strip, and is left.
    Ink connected to that edge zone is then cleared, up to `band` into the
    page (fractions of the page's short side), so a staff line that runs
    into the strip loses at most its last `band`. Changes `black` (which
    must be a writable array) in place, through views of its sides.
    """
    m = round(min(black.shape) * band)
    k = max(1, round(min(black.shape) * seed))
    # Each side as a view whose column 0 is that edge.
    for side in (black, black[:, ::-1], black.T, black.T[:, ::-1]):
        zone = side[:, :m]
        if zone[:, :k].any(axis=1).mean() < cover or side[:, m].mean() >= bleed:
            continue
        hit = zone.copy()
        hit[:, k:] = False                         # seed: ink in the edge zone
        n = np.count_nonzero(hit)
        while True:                                # grow (4-connected) inside zone
            grown = hit.copy()
            grown[1:] |= hit[:-1]; grown[:-1] |= hit[1:]
            grown[:, 1:] |= hit[:, :-1]; grown[:, :-1] |= hit[:, 1:]
            hit = grown & zone
            if n == (n := np.count_nonzero(hit)):
                break
        zone[hit] = False


def remove_specks(black, n):
    """Clear black blobs that fit inside an n x n square with white all
    round them (n=3: under 0.25 mm at 300 dpi; a staccato dot is ~7 px)."""
    r = n // 2
    inner, outer = box_sum(black, r), box_sum(black, r + 1)
    isolated = (inner > 0) & (outer == inner)      # ring around it is white
    return black & ~(box_sum(isolated, r) > 0)


def write_pdf(path, pages):
    """pages: [(image file, px w, px h, filter, bpc, w_pt, h_pt)]."""
    objs = []                                   # bytes of objects 1..N
    def add(b):
        objs.append(b); return len(objs)
    add(b"")                                    # 1: catalog (filled below)
    add(b"")                                    # 2: page tree (filled below)
    kids = []
    for img, wpx, hpx, flt, bpc, w, h in pages:
        data = Path(img).read_bytes()
        im = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                 b"/ColorSpace /DeviceGray /BitsPerComponent %d /Filter /%s "
                 b"/Length %d >>\nstream\n" % (wpx, hpx, bpc, flt.encode(), len(data))
                 + data + b"\nendstream")
        content = b"q %.4f 0 0 %.4f 0 0 cm /Im0 Do Q" % (w, h)
        c = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        kids.append(add(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.4f %.4f] "
                        b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                        % (w, h, im, c)))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = (b"<< /Type /Pages /Count %d /Kids [" % len(kids)
               + b" ".join(b"%d 0 R" % k for k in kids) + b"] >>")
    with open(path, "wb") as f:
        f.write(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
        offs = []
        for i, o in enumerate(objs, 1):
            offs.append(f.tell())
            f.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
        x = f.tell()
        f.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
        for o in offs:
            f.write(b"%010d 00000 n \n" % o)
        f.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objs) + 1, x))


def lighten(src, dst, a, boxes, mp, ppi):
    targets = [fit_a4(w, h) for w, h in boxes]
    dst_part = dst.with_name(dst.name + ".part")
    _, rot = page_geometry(src)
    tmp = Path(tempfile.mkdtemp(prefix="lighten-"))
    try:
        with ProcessPoolExecutor(a.jobs) as ex:
            # A shrunk page holds the scan's pixels in less space: its ppi rises.
            futs = [ex.submit(do_page, src, n, t[0], t[1], rot.get(n, 0),
                              ppi[n] * b[0] / t[0] if ppi.get(n) else None, tmp, a)
                    for n, (t, b) in enumerate(zip(targets, boxes), 1)]
            pages = [f.result() + t for f, t in zip(futs, targets)]
        part = tmp / "out.pdf"
        write_pdf(part, pages)
        new = page_boxes(part)
        why = []
        if len(new) != len(boxes):
            why.append(f"pages {len(boxes)} -> {len(new)}")
        else:
            off = max(max(abs(b[0] / t[0] - 1), abs(b[1] / t[1] - 1))
                      for t, b in zip(targets, new))
            shape = max(abs((b[0] / b[1]) / (o[0] / o[1]) - 1) for o, b in zip(boxes, new))
            if off > 0.001 or shape > 0.005:
                why.append(f"page size off by {max(off, shape):.1%}")
        s0, s1 = src.stat().st_size, part.stat().st_size
        if s1 >= s0 and not a.keep_larger:
            why.append(f"not smaller ({s0/1e6:.1f} -> {s1/1e6:.1f} MB)")
        if why:
            return "SKIP", "; ".join(why)
        newmp, _ = image_stats(part, len(new), a.jobs)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(part, dst_part)
        os.replace(dst_part, dst)
        shrunk = sum(t != b for t, b in zip(targets, boxes))
        return "OK", (f"{s0/1e6:.1f} -> {s1/1e6:.1f} MB, heaviest page "
                      f"{max(mp.values(), default=0):.1f} -> {max(newmp.values(), default=0):.1f} MP"
                      + (f", {shrunk} pages shrunk to A4" if shrunk else ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        dst_part.unlink(missing_ok=True)       # left only by a failed copy


def load_done(path):
    """{rel path: (size, mtime_ns, settings, status)}; the last line wins."""
    if not path.exists():
        return {}
    done = {}
    for line in path.read_text().splitlines():
        f = line.split("\t")
        if len(f) == 5:
            done[f[0]] = tuple(f[1:])
    return done


def find_pdfs(paths):
    """PDFs named, or found under folders named; .exclude folders skipped."""
    pdfs = []
    for p in paths:
        if not p.is_dir():
            pdfs.append(p)
            continue
        for d, dirs, files in os.walk(p):
            if is_excluded_dir(d):
                dirs.clear()
                continue
            dirs.sort()
            pdfs += sorted(Path(d) / f for f in files if f.lower().endswith(".pdf"))
    return pdfs


def needs_lightening(boxes, mp, min_mp):
    """(should lighten, heaviest page MP, any page bigger than A4)."""
    heaviest = max(mp.values(), default=0.0)
    oversize = any(fit_a4(w, h) != (w, h) for w, h in boxes)
    return bool(mp) and (heaviest >= min_mp or oversize), heaviest, oversize


def assess(src, jobs, min_mp):
    """Look at src: (go, heaviest MP, oversize, boxes, mp, ppi). Slow
    (image_stats decodes the images), so callers run it once a file."""
    boxes = page_boxes(src)
    mp, ppi = image_stats(src, len(boxes), jobs)
    go, heaviest, oversize = needs_lightening(boxes, mp, min_mp)
    return go, heaviest, oversize, boxes, mp, ppi


def describe(heaviest, oversize):
    return f"{heaviest:.1f} MP{', bigger than A4' if oversize else ''}"


def settings_key(a):
    """The options that change a copy; a recorded copy made with others is redone."""
    return (f"{a.mode} t={a.threshold} speck={a.speck} deskew={a.deskew} "
            f"edges={a.edges} min-mp={a.min_mp} keep-larger={a.keep_larger}")


def parse_args(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    check_args(ap, a)
    return a


def options_parser():
    """The options that say how a copy is made; music_refresh.py shares them
    (argparse parents=), then calls check_args."""
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--min-mp", type=float, default=16.7)
    ap.add_argument("--mode", choices=["bw", "gray"], default="bw")
    ap.add_argument("--threshold", default="otsu")
    ap.add_argument("--speck", type=int, default=3)
    ap.add_argument("--no-deskew", dest="deskew", action="store_false")
    ap.add_argument("--keep-edges", dest="edges", action="store_false")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--keep-larger", action="store_true")
    return ap


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 parents=[options_parser()])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("/mnt/c/Temp/Output"))
    ap.add_argument("--root", type=Path, default=Path("/mnt/z/psDATA/Resources/Music"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--redo", action="store_true")
    return ap


def check_args(ap, a):
    if a.jobs < 1:
        ap.error("--jobs must be 1 or more")
    if a.speck < 0 or (a.speck and a.speck % 2 == 0):
        ap.error("--speck must be 0 or an odd number (1, 3, 5...)")
    if a.threshold != "otsu":
        try:
            ok = 0 < float(a.threshold) < 100
        except ValueError:
            ok = False
        if not ok:
            ap.error('--threshold must be "otsu" or a percent between 0 and 100')


def main():
    a = parse_args()
    if a.mode == "bw" and not a.dry_run and not shutil.which("jbig2"):
        sys.exit("jbig2 (jbig2enc) is not installed")
    pdfs = find_pdfs(a.paths)
    out = a.out.resolve()
    settings = settings_key(a)
    done_file = a.out / ".lighten-done.tsv"
    done = {} if a.redo else load_done(done_file)
    skipped = 0
    for src in pdfs:
        src = src.resolve()
        try:
            rel = src.relative_to(a.root.resolve())
        except ValueError:
            rel = Path(src.name)
        dst = a.out / rel
        if out in src.parents or dst.resolve() == src:
            print(f"SKIP\tin --out, or would overwrite the original\t{src}", flush=True)
            continue
        st = src.stat()
        stamp = (str(st.st_size), str(st.st_mtime_ns), settings)
        prev = done.get(str(rel))
        if prev and prev[:3] == stamp and (prev[3] != "OK" or dst.exists()):
            skipped += 1
            continue
        t = time.time()
        try:
            go, heaviest, oversize, boxes, mp, ppi = assess(src, a.jobs, a.min_mp)
            if not go:
                status, detail = "LIGHT", f"{heaviest:.1f} MP, left alone"
            elif a.dry_run:
                status, detail = "WOULD", describe(heaviest, oversize)
            else:
                status, detail = lighten(src, dst, a, boxes, mp, ppi)
        except subprocess.CalledProcessError as e:      # one bad file: report it, go on
            err = " / ".join((e.stderr or b"").decode(errors="replace").split("\n")).strip()[:200]
            status, detail = "FAIL", f"{e.cmd[0]}: {err}"
        except Exception as e:
            status, detail = "FAIL", f"{type(e).__name__}: {e}"[:200]
        print(f"{status}\t{detail} ({time.time() - t:.0f} s)\t{rel}", flush=True)
        if status in ("OK", "SKIP", "LIGHT"):
            done_file.parent.mkdir(parents=True, exist_ok=True)
            with open(done_file, "a") as f:
                f.write("\t".join((str(rel), *stamp, status)) + "\n")
    if skipped:
        print(f"({skipped} files already done and unchanged; --redo to redo them)", flush=True)


if __name__ == "__main__":
    main()
