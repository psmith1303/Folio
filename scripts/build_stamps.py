#!/usr/bin/env python3
"""Generate Folio stamp SVGs from the Bravura SMuFL font.

Reads the Bravura SVG font (single file with all glyph outlines), the SMuFL
glyph-name -> codepoint map, and Bravura's metadata (per-glyph bounding boxes in
staff spaces). For each configured stamp it writes a standalone, currentColor
SVG to web/static/stamps/ and rebuilds stamps.json with each stamp's width and
height in staff spaces, so the renderer can size stamps at their true SMuFL
proportions (e.g. a repeat barline is several staff spaces tall, a crescendo
hairpin is short and wide).

Source files are cached in /tmp; pass --download to (re)fetch them.

Bravura is licensed under the SIL Open Font License.
"""

import json
import os
import re
import sys
import urllib.request

TMP = "/tmp"
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "web", "static", "stamps")

SOURCES = {
    "bravura.svg": "https://raw.githubusercontent.com/steinbergmedia/bravura/master/redist/svg/Bravura.svg",
    "bravura_metadata.json": "https://raw.githubusercontent.com/steinbergmedia/bravura/master/redist/bravura_metadata.json",
    "smufl_glyphnames.json": "https://raw.githubusercontent.com/w3c/smufl/gh-pages/metadata/glyphnames.json",
}

UPEM = 1000          # Bravura units per em
STAFF_SPACE = 250    # font units per staff space (em / 4)

# Hand-authored "V" mark for vibrato (no Bravura glyph). viewBox 220x200.
V_PATH = (
    '<path d="M22 24 L110 176 L198 24" fill="none" stroke="currentColor" '
    'stroke-width="26" stroke-linecap="round" stroke-linejoin="round"/>'
)

# id, label, spec. spec is one of:
#   ("g", smuflName)                  single glyph
#   ("gx", smuflName, stretch_x)      single glyph stretched horizontally
#   ("c", [name, ...])                compose letter glyphs side by side
#   ("svg", body, w_ss, h_ss)         hand-authored SVG body (staff-space dims)
# Ordered by likely score-markup usage: dynamics, hairpins, articulations,
# phrasing, brass effects, accidentals, ornaments, note values, clefs, then
# the less-common glissandi, arrows, scale degrees, brackets, repeats, nav.
STAMPS = [
    # Dynamics
    ("dyn-ppp", "ppp", ("c", ["dynamicPiano"] * 3)),
    ("dyn-pp", "pp", ("c", ["dynamicPiano"] * 2)),
    ("dyn-p", "p", ("c", ["dynamicPiano"])),
    ("dyn-mp", "mp", ("c", ["dynamicMezzo", "dynamicPiano"])),
    ("dyn-mf", "mf", ("c", ["dynamicMezzo", "dynamicForte"])),
    ("dyn-f", "f", ("c", ["dynamicForte"])),
    ("dyn-ff", "ff", ("c", ["dynamicForte"] * 2)),
    ("dyn-fff", "fff", ("c", ["dynamicForte"] * 3)),
    ("dyn-sf", "sf", ("c", ["dynamicSforzando", "dynamicForte"])),
    ("dyn-sfz", "sfz", ("c", ["dynamicSforzando", "dynamicForte", "dynamicZ"])),
    # Hairpins
    ("cresc", "Crescendo", ("g", "dynamicCrescendoHairpin")),
    ("dim", "Diminuendo", ("g", "dynamicDiminuendoHairpin")),
    ("cresc-wide", "Crescendo (wide)", ("gx", "dynamicCrescendoHairpin", 2.0)),
    ("dim-wide", "Diminuendo (wide)", ("gx", "dynamicDiminuendoHairpin", 2.0)),
    ("messa", "Messa di voce", ("g", "dynamicMessaDiVoce")),
    ("hairpin-bracket-left", "Hairpin bracket left", ("g", "dynamicHairpinBracketLeft")),
    ("hairpin-bracket-right", "Hairpin bracket right", ("g", "dynamicHairpinBracketRight")),
    # Articulations
    ("accent", "Accent", ("g", "articAccentAbove")),
    ("staccato", "Staccato", ("g", "articStaccatoAbove")),
    ("tenuto", "Tenuto", ("g", "articTenutoAbove")),
    ("marcato", "Marcato", ("g", "articMarcatoAbove")),
    ("marcato-staccato", "Marcato-staccato", ("g", "articMarcatoStaccatoAbove")),
    ("staccatissimo", "Staccatissimo", ("g", "articStaccatissimoAbove")),
    ("loure", "Louré", ("g", "articTenutoStaccatoAbove")),
    ("soft-accent", "Soft accent", ("g", "articSoftAccentAbove")),
    # Phrasing / breath
    ("breath", "Breath mark", ("g", "breathMarkComma")),
    ("breath-salzedo", "Breath (Salzedo)", ("g", "breathMarkSalzedo")),
    ("caesura", "Caesura", ("g", "caesura")),
    ("vibrato", "Vibrato (V)", ("svg", V_PATH, 0.88, 0.8)),
    ("fermata", "Fermata", ("g", "fermataAbove")),
    # Brass effects
    ("doit-short", "Doit short", ("g", "brassDoitShort")),
    ("doit-medium", "Doit medium", ("g", "brassDoitMedium")),
    ("doit-long", "Doit long", ("g", "brassDoitLong")),
    ("lift-short", "Smooth lift short", ("g", "brassLiftSmoothShort")),
    ("lift-medium", "Smooth lift medium", ("g", "brassLiftSmoothMedium")),
    ("lift-long", "Smooth lift long", ("g", "brassLiftSmoothLong")),
    ("mute-closed", "Brass mute closed", ("g", "brassMuteClosed")),
    ("mute-open", "Brass mute open", ("g", "brassMuteOpen")),
    # Accidentals
    ("sharp", "Sharp", ("g", "accidentalSharp")),
    ("flat", "Flat", ("g", "accidentalFlat")),
    ("natural", "Natural", ("g", "accidentalNatural")),
    ("double-sharp", "Double sharp", ("g", "accidentalDoubleSharp")),
    ("double-flat", "Double flat", ("g", "accidentalDoubleFlat")),
    # Ornaments
    ("trill", "Trill", ("g", "ornamentTrill")),
    ("turn", "Turn", ("g", "ornamentTurn")),
    ("turn-inverted", "Inverted turn", ("g", "ornamentTurnInverted")),
    ("short-trill", "Short trill", ("g", "ornamentShortTrill")),
    ("mordent", "Mordent", ("g", "ornamentMordent")),
    ("tremblement", "Tremblement", ("g", "ornamentTremblement")),
    # Note values
    ("note-whole", "Semibreve", ("g", "metNoteWhole")),
    ("note-half-up", "Minim (up)", ("g", "metNoteHalfUp")),
    ("note-half-down", "Minim (down)", ("g", "metNoteHalfDown")),
    ("note-quarter-up", "Crotchet (up)", ("g", "metNoteQuarterUp")),
    ("note-quarter-down", "Crotchet (down)", ("g", "metNoteQuarterDown")),
    ("note-8th-up", "Quaver (up)", ("g", "metNote8thUp")),
    ("note-8th-down", "Quaver (down)", ("g", "metNote8thDown")),
    ("note-16th-up", "Semiquaver (up)", ("g", "metNote16thUp")),
    ("note-16th-down", "Semiquaver (down)", ("g", "metNote16thDown")),
    # Clefs
    ("clef-g", "G clef", ("g", "gClef")),
    ("clef-c", "C clef", ("g", "cClef")),
    ("clef-f", "F clef", ("g", "fClef")),
    # Glissando
    ("gliss-up", "Glissando up", ("g", "glissandoUp")),
    ("gliss-down", "Glissando down", ("g", "glissandoDown")),
    # Arrows
    ("arrow-up", "Arrow up", ("g", "arrowOpenUp")),
    ("arrow-up-right", "Arrow up-right", ("g", "arrowOpenUpRight")),
    ("arrow-right", "Arrow right", ("g", "arrowOpenRight")),
    ("arrow-down-right", "Arrow down-right", ("g", "arrowOpenDownRight")),
    ("arrow-down", "Arrow down", ("g", "arrowOpenDown")),
    ("arrow-down-left", "Arrow down-left", ("g", "arrowOpenDownLeft")),
    ("arrow-left", "Arrow left", ("g", "arrowOpenLeft")),
    ("arrow-up-left", "Arrow up-left", ("g", "arrowOpenUpLeft")),
    # Scale degrees
    ("scale-1", "Scale degree 1", ("g", "scaleDegree1")),
    ("scale-2", "Scale degree 2", ("g", "scaleDegree2")),
    ("scale-3", "Scale degree 3", ("g", "scaleDegree3")),
    ("scale-4", "Scale degree 4", ("g", "scaleDegree4")),
    ("scale-5", "Scale degree 5", ("g", "scaleDegree5")),
    ("scale-6", "Scale degree 6", ("g", "scaleDegree6")),
    ("scale-7", "Scale degree 7", ("g", "scaleDegree7")),
    ("scale-8", "Scale degree 8", ("g", "scaleDegree8")),
    ("scale-9", "Scale degree 9", ("g", "scaleDegree9")),
    # Time signature brackets
    ("timesig-bracket-left", "Time sig bracket left", ("g", "timeSigBracketLeft")),
    ("timesig-bracket-right", "Time sig bracket right", ("g", "timeSigBracketRight")),
    # Repeats & navigation
    ("repeat-left", "Left repeat", ("g", "repeatLeft")),
    ("repeat-right", "Right repeat", ("g", "repeatRight")),
    ("repeat-both", "Left & right repeat", ("g", "repeatRightLeft")),
    ("da-capo", "Da capo", ("g", "daCapo")),
    ("segno", "Segno", ("g", "segno")),
    ("coda", "Coda", ("g", "coda")),
]


def fetch_sources(force=False):
    for name, url in SOURCES.items():
        path = os.path.join(TMP, name)
        if force or not os.path.isfile(path):
            print(f"downloading {name} ...")
            urllib.request.urlretrieve(url, path)


def load():
    with open(os.path.join(TMP, "bravura.svg"), encoding="utf-8") as f:
        font = f.read()
    gnames = json.load(open(os.path.join(TMP, "smufl_glyphnames.json")))
    meta = json.load(open(os.path.join(TMP, "bravura_metadata.json")))
    return font, gnames, meta


def parse_glyphs(font):
    """codepoint(int) -> {'d': path, 'adv': advance_in_font_units}."""
    glyphs = {}
    for el in re.findall(r"<glyph\b[^>]*?/>", font):
        m_uni = re.search(r'unicode="([^"]*)"', el)
        if not m_uni:
            continue
        cps = re.findall(r"&#x([0-9a-fA-F]+);", m_uni.group(1))
        if len(cps) != 1:   # skip ligatures; we compose our own
            continue
        cp = int(cps[0], 16)
        d = re.search(r'\sd="([^"]*)"', el)
        adv = re.search(r'horiz-adv-x="([0-9.]+)"', el)
        glyphs[cp] = {
            "d": d.group(1) if d else "",
            "adv": float(adv.group(1)) if adv else 0.0,
        }
    return glyphs


def cp_int(gnames, name):
    cp = gnames.get(name, {}).get("codepoint")  # "U+E262"
    return int(cp[2:], 16) if cp else None


def bbox_ss(meta, name):
    """Glyph bbox in staff spaces -> (x0, y0, x1, y1) or None."""
    b = meta.get("glyphBBoxes", {}).get(name)
    if not b:
        return None
    sw, ne = b["bBoxSW"], b["bBoxNE"]
    return sw[0], sw[1], ne[0], ne[1]


SVG_TMPL = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}">'
    '{body}</svg>\n'
)


def build_single(font_glyphs, gnames, meta, name, stretch_x=1.0):
    cp = cp_int(gnames, name)
    if cp is None or cp not in font_glyphs or not font_glyphs[cp]["d"]:
        return None
    bb = bbox_ss(meta, name)
    if not bb:
        return None
    x0, y0, x1, y1 = (v * STAFF_SPACE for v in bb)  # font units
    x0 *= stretch_x
    x1 *= stretch_x
    vb = f"{x0:.1f} {-y1:.1f} {x1 - x0:.1f} {y1 - y0:.1f}"
    body = (
        f'<path transform="scale({stretch_x},-1)" fill="currentColor" '
        f'd="{font_glyphs[cp]["d"]}"/>'
    )
    w_ss, h_ss = (x1 - x0) / STAFF_SPACE, (y1 - y0) / STAFF_SPACE
    return SVG_TMPL.format(vb=vb, body=body), w_ss, h_ss


def build_authored(body, w_ss, h_ss):
    vb = f"0 0 {w_ss * STAFF_SPACE:.1f} {h_ss * STAFF_SPACE:.1f}"
    return SVG_TMPL.format(vb=vb, body=body), w_ss, h_ss


def build_composite(font_glyphs, gnames, meta, names):
    parts, xoff = [], 0.0
    minx = miny = 1e9
    maxx = maxy = -1e9
    for name in names:
        cp = cp_int(gnames, name)
        bb = bbox_ss(meta, name)
        if cp is None or cp not in font_glyphs or not bb:
            return None
        gx0, gy0, gx1, gy1 = (v * STAFF_SPACE for v in bb)
        parts.append(
            f'<path transform="translate({xoff:.1f},0) scale(1,-1)" '
            f'fill="currentColor" d="{font_glyphs[cp]["d"]}"/>'
        )
        minx = min(minx, gx0 + xoff)
        maxx = max(maxx, gx1 + xoff)
        miny = min(miny, gy0)
        maxy = max(maxy, gy1)
        xoff += font_glyphs[cp]["adv"]
    vb = f"{minx:.1f} {-maxy:.1f} {maxx - minx:.1f} {maxy - miny:.1f}"
    w_ss, h_ss = (maxx - minx) / STAFF_SPACE, (maxy - miny) / STAFF_SPACE
    return SVG_TMPL.format(vb=vb, body="".join(parts)), w_ss, h_ss


# Per-stamp visual scale factor applied to each stamp's bbox size, tuned from
# user feedback at the default slider step. Baked into the manifest w/h so
# screen and PDF agree. Ornaments are tuned per-id because SMuFL bbox height is
# a poor proxy for their visual weight (a small turn glyph needs more scaling
# than a tall trill to read at the same size).
_HAIRPIN_IDS = {"cresc", "dim", "cresc-wide", "dim-wide", "messa"}
_ACCIDENTAL_IDS = {"sharp", "flat", "natural", "double-sharp", "double-flat"}
_PER_ID = {
    "trill": 1.0 / 3.0,       # confirmed right
    "turn": 2.0 / 3.0,        # 2x bigger than text default
    "turn-inverted": 2.0 / 3.0,
    "short-trill": 0.56,      # estimated to match trill's visual height
    "mordent": 0.35,
    "tremblement": 0.56,
    "da-capo": 2.0 / 3.0,     # was too small
    "vibrato": 0.7,           # authored "V" — read at about accent size
}


def factor_for(sid):
    if sid in _PER_ID:
        return _PER_ID[sid]
    if sid in _HAIRPIN_IDS:
        return 1.0           # hairpins looked right
    if sid.startswith("repeat-"):
        return 2.0 / 3.0     # 50% too big
    if sid in _ACCIDENTAL_IDS:
        return 0.5           # sharp perfect
    if sid.startswith("dyn-"):
        return 0.5           # ppp etc. were too small at 1/3
    if sid.startswith("scale-"):
        return 1.0 / 3.0     # scale degrees good
    if sid.startswith("clef-"):
        return 0.4           # clefs are tall; trim a little (estimate)
    if sid.startswith("note-"):
        return 0.45          # note values incl. stem (estimate)
    # articulations, phrasing, brass effects, brackets, arrows, gliss, nav
    return 0.5


def main():
    fetch_sources("--download" in sys.argv)
    font, gnames, meta = load()
    font_glyphs = parse_glyphs(font)
    os.makedirs(OUT_DIR, exist_ok=True)

    manifest = {"stamps": []}
    missing = []
    for sid, label, spec in STAMPS:
        kind = spec[0]
        if kind == "g":
            result = build_single(font_glyphs, gnames, meta, spec[1])
        elif kind == "gx":
            result = build_single(font_glyphs, gnames, meta, spec[1], stretch_x=spec[2])
        elif kind == "c":
            result = build_composite(font_glyphs, gnames, meta, spec[1])
        elif kind == "svg":
            result = build_authored(spec[1], spec[2], spec[3])
        else:
            result = None
        if not result:
            missing.append((sid, arg))
            continue
        svg, w_ss, h_ss = result
        f_vis = factor_for(sid)
        fname = f"{sid}.svg"
        with open(os.path.join(OUT_DIR, fname), "w", encoding="utf-8") as f:
            f.write(svg)
        manifest["stamps"].append({
            "id": sid, "label": label, "file": fname,
            "w": round(w_ss * f_vis, 3), "h": round(h_ss * f_vis, 3),
        })

    with open(os.path.join(OUT_DIR, "stamps.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    print(f"wrote {len(manifest['stamps'])} stamps to {os.path.normpath(OUT_DIR)}")
    if missing:
        print("MISSING:", missing)


if __name__ == "__main__":
    main()
