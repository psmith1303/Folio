"""Pen style grid: width x transparency (web/static/modules/annotations.js).

The toolbar chip opens a grid of widths (rows) by transparencies (columns);
one tap sets both. Widths are PDF points, so new strokes keep their size
relative to the music at any zoom, while strokes saved before widthPt keep
their screen-px `width`. The live stroke is the page redrawn with it, through
the same strokeLine as committed ink, so a translucent stroke looks the same
while drawing as after the pen lifts.

The REAL functions are sliced out of the shipped file and run under Deno with
the DOM and state stubbed (see deno_harness.py).
"""

import json

import pytest

from deno_harness import requires_deno, run_deno, slice_source
from test_annotation_tools import ANNOTATIONS_JS, UTILS_JS, _fn

pytestmark = requires_deno

W, H, PDF_W = 600, 800, 400            # CSS px per PDF pt = 1.5

PEN_FNS = ["penStyleAt", "inkAnnotation", "isPenStyle", "loadPenStylePref",
           "setPenStyle", "pageCssPerPt", "penSampleSvg", "penSample",
           "renderPenGrid", "strokeLine", "inkCssWidth", "drawInk",
           "drawPageAnnotations"]

# A 2D context that records the style of every stroke() and the path it drew.
PRELUDE = """
const __log = [];
function recordingCtx() {
  const st = { globalAlpha: 1, stack: [], path: [], strokes: [] };
  return {
    get globalAlpha() { return st.globalAlpha; },
    set globalAlpha(v) { st.globalAlpha = v; },
    setTransform() {}, clearRect() {},
    save() { st.stack.push(st.globalAlpha); },
    restore() { st.globalAlpha = st.stack.pop(); },
    beginPath() { st.path = []; },
    moveTo(x, y) { st.path.push([x, y]); },
    lineTo(x, y) { st.path.push([x, y]); },
    stroke() {
      st.strokes.push({ alpha: st.globalAlpha, width: this.lineWidth,
                        color: this.strokeStyle, points: st.path.length });
    },
    st,
  };
}
const __store = new Map();
const localStorage = {
  getItem: (k) => (__store.has(k) ? __store.get(k) : null),
  setItem: (k, v) => __store.set(k, String(v)),
};
const updatePenChip = () => __log.push("chip");
const penGrid = { innerHTML: "" };
const window = { devicePixelRatio: 1 };
"""


def _run(state: dict, body: str):
    src = ANNOTATIONS_JS.read_text(encoding="utf-8")
    consts = slice_source(src, "const PEN_WIDTHS_PT", "function penStyleAt(")
    return run_deno("\n".join([
        f'import {{ transformPt }} from "{UTILS_JS.as_uri()}";',
        PRELUDE,
        f"const __s = {json.dumps(state)};",
        "const getState = () => __s;",
        "const ANNOT_TYPES = { ink: { draw: drawInk } };",
        consts,
        *(_fn(src, n) for n in PEN_FNS),
        body,
    ]))


def _state(**kw) -> dict:
    s = {"penColor": "red", "penStyle": {"row": 1, "col": 0},
         "pageLayouts": [{"page": 1, "cssW": W, "cssH": H, "pdfW": PDF_W}],
         "annotations": {}, "rotations": {}, "currentStroke": [],
         "strokeLayoutIndex": None}
    s.update(kw)
    return s


def test_grid_is_four_widths_by_three_transparencies_with_a_wide_highlighter():
    r = _run(_state(), """
const out = [];
for (let row = 0; row < 4; row++) for (let col = 0; col < 3; col++) {
  out.push(penStyleAt(row, col));
}
console.log(JSON.stringify(out));
""")
    widths = [[r[row * 3 + col]["widthPt"] for col in range(3)] for row in range(4)]
    assert widths == [[0.75, 0.75, 3], [1.5, 1.5, 6], [3, 3, 12], [6, 6, 24]]
    assert {c["opacity"] for c in r[0::3]} == {1}
    assert {c["opacity"] for c in r[1::3]} == {0.6}
    assert {c["opacity"] for c in r[2::3]} == {0.3}


@pytest.mark.parametrize("annot, width", [
    ({"widthPt": 4, "width": 99}, 6),     # page-relative: 4pt at 1.5 px/pt
    ({"width": 5}, 5),                    # saved before widthPt: screen px
    ({}, 2),                              # no width at all
])
def test_ink_width_scales_with_the_page_only_when_stored_in_points(annot, width):
    r = _run(_state(), f"""
const ctx = recordingCtx();
drawInk(ctx, {{ points: [[0, 0], [0.5, 0.5]], ...{json.dumps(annot)} }}, {W}, {H}, 0, {PDF_W});
console.log(JSON.stringify(ctx.st.strokes));
""")
    assert r[0]["width"] == pytest.approx(width)


def test_ink_opacity_is_applied_to_its_stroke_and_restored_after():
    r = _run(_state(), f"""
const ctx = recordingCtx();
drawInk(ctx, {{ points: [[0, 0], [0.5, 0.5]], opacity: 0.3 }}, {W}, {H}, 0, {PDF_W});
drawInk(ctx, {{ points: [[0, 0], [0.5, 0.5]] }}, {W}, {H}, 0, {PDF_W});
console.log(JSON.stringify({{ strokes: ctx.st.strokes, after: ctx.globalAlpha }}));
""")
    assert [s["alpha"] for s in r["strokes"]] == [0.3, 1]
    assert r["after"] == 1


@pytest.mark.parametrize("style, expected", [
    ({"row": 1, "col": 0}, {"widthPt": 1.5, "width": 2}),                   # 2.25px
    ({"row": 3, "col": 2}, {"widthPt": 24, "width": 36, "opacity": 0.3}),
])
def test_a_committed_stroke_stores_points_width_a_px_fallback_and_opacity(style, expected):
    r = _run(_state(), f"""
const a = inkAnnotation([[0.1, 0.1], [0.2, 0.2]], "red", {json.dumps(style)}, __s.pageLayouts[0]);
console.log(JSON.stringify(a));
""")
    assert r["type"] == "ink" and r["color"] == "red"
    assert {k: r.get(k) for k in expected} == expected
    if "opacity" not in expected:
        assert "opacity" not in r       # solid strokes stay as they always were


def test_the_pen_style_is_remembered():
    r = _run(_state(), """
setPenStyle(3, 2);
console.log(JSON.stringify({ state: __s.penStyle, loaded: loadPenStylePref(), log: __log }));
""")
    assert r["state"] == r["loaded"] == {"row": 3, "col": 2}
    assert r["log"] == ["chip"]


@pytest.mark.parametrize("stored", [None, "not json", '{"row": 9, "col": 0}',
                                    '{"row": 1}', '{"row": "1", "col": 0}'])
def test_a_missing_or_garbled_pen_style_falls_back_to_medium_solid(stored):
    body = "" if stored is None else f"__store.set('folio.penStyle', {json.dumps(stored)});"
    r = _run(_state(), body + "console.log(JSON.stringify(loadPenStylePref()));")
    assert r == {"row": 1, "col": 0}


def test_the_live_stroke_looks_exactly_like_the_committed_one():
    """Regression guard: the preview used to add one segment per move, which
    at partial opacity darkens every joint and then jumps when committed."""
    stroke = [{"x": 60, "y": 80}, {"x": 120, "y": 160}, {"x": 180, "y": 200}]
    r = _run(_state(penStyle={"row": 2, "col": 2}, currentStroke=stroke,
                    strokeLayoutIndex=0), f"""
const layout = __s.pageLayouts[0];
const live = recordingCtx();
drawPageAnnotations({{ getContext: () => live }}, layout, 0);
const norm = __s.currentStroke.map(({{ x, y }}) => [x / {W}, y / {H}]);
const done = recordingCtx();
drawInk(done, inkAnnotation(norm, __s.penColor, __s.penStyle, layout), {W}, {H}, 0, {PDF_W});
console.log(JSON.stringify({{ live: live.st.strokes, done: done.st.strokes }}));
""")
    assert len(r["live"]) == 1                      # one path, not per segment
    assert r["live"][0]["points"] == 3
    assert r["live"] == r["done"]
    assert r["live"][0]["alpha"] == 0.3


def test_the_live_stroke_is_drawn_only_on_its_own_page():
    stroke = [{"x": 60, "y": 80}, {"x": 120, "y": 160}]
    r = _run(_state(currentStroke=stroke, strokeLayoutIndex=1), """
const ctx = recordingCtx();
drawPageAnnotations({ getContext: () => ctx }, __s.pageLayouts[0], 0);
console.log(JSON.stringify(ctx.st.strokes));
""")
    assert r == []


def test_the_grid_shows_every_cell_at_its_on_page_size_and_marks_the_current_one():
    r = _run(_state(penStyle={"row": 0, "col": 1}), """
renderPenGrid();
const cells = [...penGrid.innerHTML.matchAll(
  /class="pen-cell( selected)?" data-row="(\\d)" data-col="(\\d)".*?stroke-width="([\\d.]+)" stroke-opacity="([\\d.]+)"/g,
)].map((m) => ({ sel: !!m[1], row: +m[2], col: +m[3], w: +m[4], a: +m[5] }));
console.log(JSON.stringify(cells));
""")
    assert len(r) == 12
    assert [(c["row"], c["col"]) for c in r if c["sel"]] == [(0, 1)]
    by = {(c["row"], c["col"]): c for c in r}
    assert by[(1, 0)]["w"] == pytest.approx(2.25)   # 1.5pt at 1.5 px/pt
    assert by[(3, 2)]["w"] == 28                    # 36px capped to fit the cell
    assert by[(3, 2)]["a"] == 0.3


# ---------------------------------------------------------------------------
# Wiring: the whole module, with its siblings stubbed (js_module_harness.py)
# ---------------------------------------------------------------------------

WIRING_SETUP = """
const __store = new Map([["folio.penStyle", '{"row":2,"col":1}']]);
// Deno has a real, persistent localStorage; replace it so no test writes it.
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
  getItem: (k) => (__store.has(k) ? __store.get(k) : null),
  setItem: (k, v) => __store.set(k, String(v)),
} });
document.querySelectorAll = () => [];
const __qs = document.querySelector;
document.querySelector = (sel) => (sel === ".swatch.selected" ? null : __qs(sel));
for (const c of ["annotCanvas1", "annotCanvas2"]) __el(c).style = {};
const dlg = __el("penDialog");
dlg.showModal = () => { dlg.open = true; __log.push("showModal"); };
dlg.close = () => { dlg.open = false; __log.push("close"); };
"""


def _wiring(body: str):
    from js_module_harness import MODULES as MODS, run_module
    state = {"activeTool": "nav", "penColor": "blue", "penStyle": {"row": 1, "col": 0},
             "pageLayouts": [], "currentStroke": []}
    return run_module(
        MODS / "annotations.js",
        ["dom", "state", "api", "stamps", "dialog-handlers", "viewer", "annot-outbox"],
        "M.initAnnotationEvents();\n" + body, state=state, setup=WIRING_SETUP)


def test_the_saved_pen_style_is_restored_and_shown_on_the_chip():
    r = _wiring("""
console.log(JSON.stringify({ style: __state.penStyle, chip: __el("btnPenStyle").innerHTML,
                             title: __el("btnPenStyle").title }));
""")
    assert r["style"] == {"row": 2, "col": 1}
    assert 'stroke="blue"' in r["chip"] and 'stroke-opacity="0.6"' in r["chip"]
    assert r["title"] == "Pen: Bold, Semi"


def test_picking_a_cell_sets_the_style_closes_the_grid_and_arms_the_pen():
    r = _wiring("""
__el("btnPenStyle").listeners.click();
const opened = __el("penDialog").open && __el("penGrid").innerHTML.includes("pen-cell");
const cell = { dataset: { row: "3", col: "2" } };
__el("penGrid").listeners.click({ target: { closest: (sel) => (sel === ".pen-cell" ? cell : null) } });
console.log(JSON.stringify({ opened, open: __el("penDialog").open, style: __state.penStyle,
                             tool: __state.activeTool, saved: __store.get("folio.penStyle") }));
""")
    assert r["opened"] is True
    assert r["open"] is False
    assert r["style"] == {"row": 3, "col": 2}
    assert r["tool"] == "pen"
    assert json.loads(r["saved"]) == {"row": 3, "col": 2}


def test_a_click_between_cells_changes_nothing():
    r = _wiring("""
__el("btnPenStyle").listeners.click();
__el("penGrid").listeners.click({ target: { closest: () => null } });
console.log(JSON.stringify({ open: __el("penDialog").open, style: __state.penStyle,
                             tool: __state.activeTool }));
""")
    assert r == {"open": True, "style": {"row": 2, "col": 1}, "tool": "nav"}
