"""Annotation hit-testing and placement tools (web/static/modules/annotations.js).

The Step 6 refactor put per-type draw/hit functions in an ANNOT_TYPES table,
the topmost-hit search in findTopHit, and the stamp/start-page tools in a
PLACEMENT_TOOLS table. These tests pin the behaviour those pieces must keep.

annotations.js imports dom.js (which queries the document at import), so the
REAL functions are sliced out of the shipped file and run under Deno with only
the DOM, state and I/O stubbed; see deno_harness.py.
"""

import json
from pathlib import Path

import pytest

from deno_harness import requires_deno, run_deno, slice_source

pytestmark = requires_deno

MODULES = Path(__file__).resolve().parent.parent / "web" / "static" / "modules"
ANNOTATIONS_JS = MODULES / "annotations.js"
UTILS_JS = MODULES / "utils.js"

W, H, PDF_W = 600, 800, 612          # page size in CSS px; PDF width in pt

# Pure geometry: size helpers, text layout, per-type hit tests and the table.
GEOMETRY = ["textCssSize", "stampCssSize", "startStampCssSize", "textLayout",
            "hitInk", "hitText", "hitStamp", "hitStartStamp"]


def _fn(src: str, name: str) -> str:
    return slice_source(src, f"function {name}(", "\n}\n") + "\n}\n"


def _const(src: str, name: str) -> str:
    return slice_source(src, f"const {name} = ", "\n};\n") + "\n};\n"


DRAW_FNS = ["drawInk", "drawText", "drawStamp", "drawStartStamp"]


def _geometry(src: str, real: tuple[str, ...] = ()) -> str:
    """The geometry functions plus ANNOT_TYPES. Draw functions are stubbed
    unless named in *real* (the caller then appends the real ones)."""
    return "\n".join([
        f'import {{ transformPt, inverseTransformPt, sizeToPt, cssPerPt, NOTE_GLYPHS }} '
        f'from "{UTILS_JS.as_uri()}";',
        # Stamp metadata in staff spaces, as stamps.js would supply it.
        "const getStampMeta = (id) => ({ w: id === 'wide' ? 4 : 1, h: 2 });",
        "const START_STAMP_PT = 20 / 25.4 * 72;",
        *(f"const {n} = null;" for n in DRAW_FNS if n not in real),
        *(_fn(src, n) for n in GEOMETRY),
        _const(src, "ANNOT_TYPES"),
    ])


def _run(body: str, prelude: str = ""):
    src = ANNOTATIONS_JS.read_text(encoding="utf-8")
    return run_deno(_geometry(src) + "\n" + prelude + "\n" + body)


def _hit(annot: dict, px: float, py: float, rot: int = 0, halo: int = 20) -> bool:
    return _run(f"""
const a = {json.dumps(annot)};
console.log(JSON.stringify(ANNOT_TYPES[a.type].hit(a, {px}, {py}, {W}, {H}, {rot}, {halo}, {PDF_W})));
""")


# ---------------------------------------------------------------------------
# Per-type hit tests
# ---------------------------------------------------------------------------

INK = {"type": "ink", "points": [[0.1, 0.1], [0.5, 0.5]]}


@pytest.mark.parametrize("px, py, expected", [
    (300, 400, True),          # on a stroke point (0.5, 0.5)
    (315, 415, True),          # within the 20 px halo
    (321, 400, False),         # just outside it
    (200, 250, False),         # between points: ink hits test points, not segments
])
def test_ink_hits_near_its_points(px, py, expected):
    assert _hit(INK, px, py) is expected


def test_ink_hit_follows_page_rotation():
    """A stored point is found where the rotated page draws it."""
    rotated = _run(f"""
const [cx, cy] = transformPt(0.1, 0.1, {W}, {H}, 90);
const a = {json.dumps(INK)};
console.log(JSON.stringify([ANNOT_TYPES.ink.hit(a, cx, cy, {W}, {H}, 90, 20, {PDF_W}),
                            ANNOT_TYPES.ink.hit(a, 60, 80, {W}, {H}, 90, 20, {PDF_W})]));
""")
    assert rotated == [True, False]


def test_small_stamp_is_hittable_within_the_halo():
    """Stamp hit boxes are at least the halo in each direction."""
    tiny = {"type": "stamp", "id": "narrow", "x": 0.5, "y": 0.5, "size": 0}
    assert _hit(tiny, 300 + 19, 400 - 19)
    assert not _hit(tiny, 300 + 21, 400)


def test_wide_stamp_hits_across_its_width():
    wide = {"type": "stamp", "id": "wide", "x": 0.5, "y": 0.5, "size": 12}
    half_w = _run("""
const { wCss } = stampCssSize("wide", 12, 600, 612);
console.log(JSON.stringify(wCss / 2));
""")
    assert half_w > 20
    assert _hit(wide, 300 + half_w - 1, 400)
    assert not _hit(wide, 300 + half_w + 1, 400)


def test_start_stamp_hit_box_is_its_20mm_square():
    half = _run("console.log(JSON.stringify(startStampCssSize(600, 612) / 2));")
    stamp = {"type": "startpage", "x": 0.5, "y": 0.5}
    assert _hit(stamp, 300 + half - 1, 400 + half - 1)
    assert not _hit(stamp, 300 + half + 1, 400)


def test_single_line_text_hits_over_the_drawn_text():
    text = {"type": "text", "x": 0.5, "y": 0.5, "text": "cresc.", "size": 5}
    assert _hit(text, 310, 395)          # inside the text, above the baseline
    assert not _hit(text, 300 - 21, 395)  # left of the anchor beyond the halo


MULTI = {"type": "text", "x": 0.5, "y": 0.5, "text": "one\ntwo\nthree", "size": 5}


def _text_probe(annot: dict, expr: str, rot: int = 0):
    """Evaluate *expr* with the text's layout (cx, cy, sz, lines, lineH) and
    hit(px, py) (20 px halo) in scope, on a page rotated by *rot*."""
    return _run(f"""
const a = {json.dumps(annot)};
const {{ cx, cy, sz, lines, lineH }} = textLayout(a, {W}, {H}, {rot}, {PDF_W});
const hit = (px, py) => ANNOT_TYPES.text.hit(a, px, py, {W}, {H}, {rot}, 20, {PDF_W});
console.log(JSON.stringify({expr}));
""")


ROTATIONS = pytest.mark.parametrize("rot", [0, 90, 180, 270])


@ROTATIONS
def test_every_line_of_multiline_text_is_hittable(rot):
    """Regression: drawText puts each further line lower down, but the hit
    box grew *upwards* from the anchor, so on 3-line text the last line
    couldn't be erased, moved or tapped to edit at all. Text is drawn
    upright whatever the page rotation, so this holds on rotated pages too."""
    probes = _text_probe(MULTI, "lines.map((_, i) => hit(cx + 5, cy + i * lineH - sz / 2))", rot)
    assert probes == [True, True, True]


@ROTATIONS
def test_blank_space_above_multiline_text_is_not_a_hit(rot):
    """Regression: the box extended lines.length line heights above the
    anchor, where nothing is drawn."""
    assert _text_probe(MULTI, "hit(cx + 5, cy - 2.5 * lineH)", rot) is False


def test_multiline_text_box_ends_a_halo_below_the_last_line():
    edges = _text_probe(MULTI, "[hit(cx + 5, cy + 2 * lineH + 19), hit(cx + 5, cy + 2 * lineH + 21)]")
    assert edges == [True, False]


def test_single_line_text_box_is_unchanged():
    """One line height above the baseline to the baseline, plus the halo --
    what the box has always been for single-line text."""
    one = {"type": "text", "x": 0.5, "y": 0.5, "text": "cresc.", "size": 5}
    edges = _text_probe(one, "[hit(cx + 5, cy - lineH - 19), hit(cx + 5, cy - lineH - 21),"
                             " hit(cx + 5, cy + 19), hit(cx + 5, cy + 21)]")
    assert edges == [True, False, True, False]


def test_draw_and_hit_share_one_text_layout():
    """drawText places its lines from textLayout, so the eraser/move target
    and the drawing can't drift apart."""
    src = ANNOTATIONS_JS.read_text(encoding="utf-8")
    drawn = run_deno(_geometry(src, real=("drawText",)) + "\n" + _fn(src, "drawText") + """
const calls = [];
const ctx = { fillText: (t, x, y) => calls.push([t, x, y]) };
const a = { type: "text", x: 0.25, y: 0.75, text: "a\\nbb", size: 7 };
drawText(ctx, a, 600, 800, 90, 612);
const L = textLayout(a, 600, 800, 90, 612);
console.log(JSON.stringify({ calls, expected: L.lines.map((t, i) => [t, L.cx, L.cy + i * L.lineH]) }));
""")
    assert drawn["calls"] == drawn["expected"]


# ---------------------------------------------------------------------------
# findTopHit
# ---------------------------------------------------------------------------

FIND = """
const __s = { annotations: %s };
const getState = () => __s;
const layout = { cssW: 600, cssH: 800, pdfW: 612 };
const p = (x, y) => ({ x, y, pg: "0", rot: 0 });
"""


def _find(annots: list[dict], calls: str):
    src = ANNOTATIONS_JS.read_text(encoding="utf-8")
    return _run(f"console.log(JSON.stringify({calls}));",
                prelude=FIND % json.dumps({"0": annots}) + _fn(src, "findTopHit"))


def test_topmost_annotation_wins():
    under = {**INK, "uuid": "under"}
    over = {**INK, "uuid": "over"}
    assert _find([under, over], "findTopHit(p(300, 400), layout, 20).uuid") == "over"


def test_unknown_type_on_top_is_skipped():
    """An annotation type this client doesn't know is never hit (nor drawn),
    so the known one underneath is found."""
    mystery = {"type": "mystery", "uuid": "m", "x": 0.5, "y": 0.5}
    assert _find([{**INK, "uuid": "ink"}, mystery],
                 "findTopHit(p(300, 400), layout, 20).uuid") == "ink"


def test_type_filter_looks_past_other_types():
    """The text tool edits the text under the pointer even when ink is drawn
    over it."""
    text = {"type": "text", "uuid": "t", "x": 0.5, "y": 0.5, "text": "p", "size": 5}
    got = _find([text, {**INK, "uuid": "ink"}],
                "[findTopHit(p(302, 395), layout, 10).uuid,"
                " findTopHit(p(302, 395), layout, 10, 'text').uuid]")
    assert got == ["ink", "t"]


@pytest.mark.parametrize("halo, only", [(10, "text"), (20, None)])
def test_tapping_the_last_line_of_multiline_text_finds_it(halo, only):
    """The user-visible symptom, through the paths the tools use: the text
    tool (10 px, text only) and the eraser/move tool (20 px, any type)."""
    src = ANNOTATIONS_JS.read_text(encoding="utf-8")
    text = {**MULTI, "uuid": "t"}
    found = _run(f"""
const {{ cx, cy, sz, lineH }} = textLayout({json.dumps(text)}, 600, 800, 0, 612);
const hit = findTopHit(p(cx + 5, cy + 2 * lineH - sz / 2), layout, {halo}, {json.dumps(only)});
console.log(JSON.stringify(hit && hit.uuid));
""", prelude=FIND % json.dumps({"0": [text]}) + _fn(src, "findTopHit"))
    assert found == "t"


@pytest.mark.parametrize("annots, point", [
    ([], "p(300, 400)"),                  # empty page
    ([INK], "p(10, 790)"),                # nothing under the pointer
])
def test_no_hit_is_null(annots, point):
    assert _find(annots, f"findTopHit({point}, layout, 20)") is None


def test_page_without_annotations_is_null():
    src = ANNOTATIONS_JS.read_text(encoding="utf-8")
    assert _run("console.log(JSON.stringify(findTopHit({ x: 1, y: 1, pg: '7', rot: 0 }, layout, 20)));",
                prelude=FIND % "{}" + _fn(src, "findTopHit")) is None


# ---------------------------------------------------------------------------
# Placement tools: stamp and start page, through onPointerDown
# ---------------------------------------------------------------------------

POINTER = """
const __s = %(state)s;
const getState = () => __s;
const log = [];
const setTool = (t) => { __s.activeTool = t; log.push("tool:" + t); };
const saveAnnotations = () => log.push("save");
const drawAnnotations = () => log.push("draw");
const pushUndo = (pg) => log.push("undo:" + pg);
const sizeSlider = { value: "9" };
let __n = 0;
const crypto = { randomUUID: () => "u" + (++__n) };
const stampToolCursor = () => "", startPageToolCursor = () => "";
const handleTextClick = () => log.push("text");
const eraseAt = () => {}, startMove = () => false;
const nextPage = () => {}, prevPage = () => {};
const canvas = { getBoundingClientRect: () => ({ left: 0, top: 0 }), setPointerCapture() {} };
const tap = (x, y) => onPointerDown(
  { clientX: x, clientY: y, pointerType: "mouse", pointerId: 1, preventDefault() {} }, canvas, 0);
"""

PLACEMENT_FNS = ["canvasCoords", "pagePoint", "isPlacementTool", "placeStamp",
                 "placeStartPage", "onPointerDown"]


def _tap(state: dict, taps: str) -> dict:
    base = {"activeTool": "nav", "displayMode": "fit", "pencilOnly": False,
            "pageLayouts": [{"page": 1, "cssW": 600, "cssH": 800, "pdfW": 612}],
            "rotations": {}, "annotations": {}, "selectedStamp": None,
            "penColor": "red", "currentStroke": []}
    src = ANNOTATIONS_JS.read_text(encoding="utf-8")
    prelude = (POINTER % {"state": json.dumps({**base, **state})}
               + _const(src, "PLACEMENT_TOOLS")
               + "\n".join(_fn(src, n) for n in PLACEMENT_FNS))
    return _run(f"""{taps};
console.log(JSON.stringify({{ log, tool: __s.activeTool, annotations: __s.annotations }}));
""", prelude=prelude)


def test_stamp_tap_places_the_selected_stamp_then_returns_to_nav():
    r = _tap({"activeTool": "stamp", "selectedStamp": "fermata"}, "tap(300, 200)")
    assert r["tool"] == "nav"
    assert r["log"] == ["undo:0", "save", "tool:nav", "draw"]
    assert r["annotations"] == {"0": [{
        "uuid": "u1", "type": "stamp", "id": "fermata", "x": 0.5, "y": 0.25,
        "size": 9, "color": "red"}]}


def test_stamp_tap_without_a_selection_does_nothing():
    """Nothing is placed, saved or undone, and the tool stays armed."""
    r = _tap({"activeTool": "stamp"}, "tap(300, 200)")
    assert r == {"log": [], "tool": "stamp", "annotations": {}}


def test_start_page_tap_places_one_stamp_then_returns_to_nav():
    r = _tap({"activeTool": "startpage"}, "tap(150, 600)")
    assert r["tool"] == "nav"
    assert r["log"] == ["undo:0", "save", "tool:nav", "draw"]
    assert r["annotations"] == {"0": [
        {"uuid": "u1", "type": "startpage", "x": 0.25, "y": 0.75}]}


def test_placing_the_start_page_again_moves_it_between_pages():
    """One per document: the old stamp goes (undoable on its own page) and
    the page's other annotations stay."""
    ink = {**INK, "uuid": "ink"}
    r = _tap({"activeTool": "startpage", "annotations": {
        "3": [ink, {"uuid": "old", "type": "startpage", "x": 0.1, "y": 0.1}],
        "0": [{"uuid": "here", "type": "startpage", "x": 0.9, "y": 0.9}],
    }}, "tap(300, 400)")
    assert r["annotations"]["3"] == [ink]
    assert r["annotations"]["0"] == [
        {"uuid": "u1", "type": "startpage", "x": 0.5, "y": 0.5}]
    assert r["log"] == ["undo:3", "undo:0", "save", "tool:nav", "draw"]


def test_placement_follows_page_rotation():
    """The stored point is in unrotated page space."""
    r = _tap({"activeTool": "startpage", "rotations": {"0": 90}}, "tap(150, 200)")
    [stamp] = r["annotations"]["0"]
    back = run_deno(f"""
import {{ transformPt }} from "{UTILS_JS.as_uri()}";
console.log(JSON.stringify(transformPt({stamp['x']}, {stamp['y']}, 600, 800, 90).map(Math.round)));
""")
    assert back == [150, 200]


@pytest.mark.parametrize("tool, expected", [
    ("stamp", True), ("startpage", True),
    ("nav", False), ("pen", False), ("text", False),
    ("toString", False), ("constructor", False),   # not fooled by Object.prototype
])
def test_is_placement_tool(tool, expected):
    src = ANNOTATIONS_JS.read_text(encoding="utf-8")
    prelude = ("const stampToolCursor = null, startPageToolCursor = null, "
               "placeStamp = null, placeStartPage = null;\n"
               + _const(src, "PLACEMENT_TOOLS") + _fn(src, "isPlacementTool"))
    assert _run(f"console.log(JSON.stringify(isPlacementTool({json.dumps(tool)})));",
                prelude=prelude) is expected
