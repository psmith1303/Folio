"""Pen style grid: width x transparency (web/static/modules/annotations.js).

The toolbar chip opens a grid of widths (rows) by transparencies (columns);
one tap sets both. Widths are PDF points, so new strokes keep their size
relative to the music at any zoom, while strokes saved before widthPt keep
their screen-px `width`. A translucent stroke keeps its transparency in its
colour (#rrggbbaa), which clients older than the grid also draw translucent. The live stroke is the page redrawn with it, through
the same strokeLine as committed ink, so a translucent stroke looks the same
while drawing as after the pen lifts.

The grid, chip and saved choice are in pen-style.js; drawing and committing
strokes in annotations.js. The REAL functions are sliced out of the shipped
files and run under Deno with the DOM and state stubbed (see
deno_harness.py).
"""

import json

import pytest

from deno_harness import requires_deno, run_deno, slice_source
from test_annotation_tools import ANNOTATIONS_JS, MODULES, UTILS_JS, _fn

pytestmark = requires_deno

W, H, PDF_W = 600, 800, 400            # CSS px per PDF pt = 1.5

PEN_STYLE_JS = MODULES / "pen-style.js"
VIEWER_JS = MODULES / "viewer.js"

PEN_STYLE_FNS = ["penStyleAt", "isPenStyle", "loadPenStylePref", "setPenStyle",
                 "penSampleSvg", "penSample", "updatePenChip", "renderPenGrid"]
DRAW_FNS = ["inkColor", "inkAnnotation", "strokeLine", "inkCssWidth", "drawInk",
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
// Deno has a real, persistent localStorage; replace it so no test writes it.
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
  getItem: (k) => (__store.has(k) ? __store.get(k) : null),
  setItem: (k, v) => __store.set(k, String(v)),
} });
const btnPenStyle = { innerHTML: "", title: "" };
const penGrid = { innerHTML: "" };
// A 2D context's fillStyle normalises opaque colours to #rrggbb, as browsers do.
const __hex = { red: "#ff0000", yellow: "#ffff00", black: "#000000" };
const document = { createElement: () => ({ getContext: () => {
  let v = "#000000";
  return { get fillStyle() { return v; },
           set fillStyle(c) { v = __hex[c] || (c.startsWith("#") ? c : "rgba(1, 2, 3, 0.5)"); } };
} }) };
const window = { devicePixelRatio: 1 };
"""


def _run(state: dict, body: str):
    pen = PEN_STYLE_JS.read_text(encoding="utf-8")
    draw = ANNOTATIONS_JS.read_text(encoding="utf-8")
    return run_deno("\n".join([
        f'import {{ transformPt, cssPerPt, readPref, writePref }} from "{UTILS_JS.as_uri()}";',
        PRELUDE,
        f"const __s = {json.dumps(state)};",
        "const getState = () => __s;",
        "const ANNOT_TYPES = { ink: { draw: drawInk } };",
        slice_source(pen, "const PEN_WIDTHS_PT", "export function penStyleAt("),
        slice_source(draw, "let _colorCtx", ";\n") + ";",
        *(_fn(pen, n) for n in PEN_STYLE_FNS),
        *(_fn(draw, n) for n in DRAW_FNS),
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


def test_an_opacity_field_from_v2_16_0_still_draws_translucent_and_is_restored_after():
    r = _run(_state(), f"""
const ctx = recordingCtx();
drawInk(ctx, {{ points: [[0, 0], [0.5, 0.5]], opacity: 0.3 }}, {W}, {H}, 0, {PDF_W});
drawInk(ctx, {{ points: [[0, 0], [0.5, 0.5]] }}, {W}, {H}, 0, {PDF_W});
console.log(JSON.stringify({{ strokes: ctx.st.strokes, after: ctx.globalAlpha }}));
""")
    assert [s["alpha"] for s in r["strokes"]] == [0.3, 1]
    assert r["after"] == 1


@pytest.mark.parametrize("style, expected", [
    ({"row": 1, "col": 0}, {"widthPt": 1.5, "width": 2, "color": "red"}),   # 2.25px
    ({"row": 3, "col": 2}, {"widthPt": 24, "width": 36, "color": "#ff00004d"}),
])
def test_a_committed_stroke_stores_points_width_a_px_fallback_and_its_colour(style, expected):
    """Clients older than the grid ignore widthPt and opacity but draw
    `width` px in `color`, so a highlight still shows translucent there."""
    r = _run(_state(), f"""
const a = inkAnnotation([[0.1, 0.1], [0.2, 0.2]], "red", {json.dumps(style)}, __s.pageLayouts[0]);
console.log(JSON.stringify(a));
""")
    assert r["type"] == "ink"
    assert {k: r.get(k) for k in expected} == expected
    assert "opacity" not in r


@pytest.mark.parametrize("color, opacity, expected", [
    ("red", 1, "red"),                  # solid: stored as picked, as always
    ("red", 0.6, "#ff000099"),
    ("yellow", 0.3, "#ffff004d"),
    ("oddcolour", 0.3, "oddcolour"),    # not normalisable to #rrggbb: as is
])
def test_ink_colour_carries_the_transparency(color, opacity, expected):
    r = _run(_state(), f"console.log(JSON.stringify(inkColor({json.dumps(color)}, {opacity})));")
    assert r == expected


@pytest.mark.parametrize("layouts, width", [
    ([{"page": 1, "cssW": W, "cssH": H, "pdfW": PDF_W}], 2.25),   # 1.5pt at 1.5 px/pt
    ([], 1.5),                                                    # no page yet: 1 px/pt
])
def test_the_chip_draws_the_stroke_at_the_page_scale(layouts, width):
    r = _run(_state(pageLayouts=layouts), """
updatePenChip();
console.log(JSON.stringify({ html: btnPenStyle.innerHTML, title: btnPenStyle.title }));
""")
    assert f'stroke-width="{width}"' in r["html"]
    assert r["title"] == "Pen: Medium, Solid"


def test_the_pen_style_is_remembered():
    r = _run(_state(), """
setPenStyle(3, 2);
console.log(JSON.stringify({ state: __s.penStyle, loaded: loadPenStylePref(),
                             chip: btnPenStyle.title }));
""")
    assert r["state"] == r["loaded"] == {"row": 3, "col": 2}
    assert r["chip"] == "Pen: Heavy, Highlight"


@pytest.mark.parametrize("stored", [None, "not json", '{"row": 9, "col": 0}',
                                    '{"row": 1}', '{"row": "1", "col": 0}'])
def test_a_missing_or_garbled_pen_style_falls_back_to_medium_solid(stored):
    body = "" if stored is None else f"__store.set('folio.penStyle', {json.dumps(stored)});"
    r = _run(_state(), body + "console.log(JSON.stringify(loadPenStylePref()));")
    assert r == {"row": 1, "col": 0}


def test_the_live_stroke_looks_exactly_like_the_committed_one():
    """Regression guard: the preview used to add one segment per move, which
    at partial opacity darkens every joint and then jumps when committed."""
    stroke = [[60, 80], [120, 160], [180, 200]]
    r = _run(_state(penStyle={"row": 2, "col": 2}, currentStroke=stroke,
                    strokeLayoutIndex=0), f"""
const layout = __s.pageLayouts[0];
const live = recordingCtx();
drawPageAnnotations({{ getContext: () => live }}, layout, 0);
const norm = __s.currentStroke.map(([x, y]) => [x / {W}, y / {H}]);
const done = recordingCtx();
drawInk(done, inkAnnotation(norm, __s.penColor, __s.penStyle, layout), {W}, {H}, 0, {PDF_W});
console.log(JSON.stringify({{ live: live.st.strokes, done: done.st.strokes }}));
""")
    assert len(r["live"]) == 1                      # one path, not per segment
    assert r["live"][0]["points"] == 3
    assert r["live"] == r["done"]
    assert r["live"][0]["color"] == "#ff00004d"


def test_the_live_stroke_is_drawn_only_on_its_own_page():
    stroke = [[60, 80], [120, 160]]
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


# ---------------------------------------------------------------------------
# Bug fix A: renderPage() must refresh the toolbar chip once the page it
# samples from is actually laid out, not just at startup / colour change /
# style pick, when no page is open yet and the chip's sample draws at scale
# 1 -- stale as soon as a page with a different px/pt ratio is shown.
#
# renderPage lives in viewer.js and calls out to a dozen collaborators
# (rendering, prerendering, toasts); slicing it out and stubbing every
# collaborator is the only practical way to run the REAL function, per
# test_offline_copy_kept_on_failed_load.py's approach to the same file.
# ---------------------------------------------------------------------------

RENDER_PAGE_FAKES = """
const __log = [];
function dbg() {}
const VIEWER_TAG = "[viewer]";
const pageInput = { value: null };
const pageWrap2 = { classList: { add() {}, remove() {} } };
const canvas1 = {}, annotCanvas1 = {};
const canvas2 = { width: 1, height: 1 }, annotCanvas2 = { width: 1, height: 1 };
const pdfContainer = { scrollTop: 0, scrollHeight: 999 };
function hideToast() { __log.push("hideToast"); }
function cleanupOldPages() { __log.push("cleanupOldPages"); }
function cleanupAllPages() { __log.push("cleanupAllPages"); }
function showToast(msg) { __log.push("showToast:" + msg); }
async function prerenderNeighbors() { __log.push("prerenderNeighbors"); }
async function prefetchNextSetlistSong() { __log.push("prefetchNextSetlistSong"); }
function drawAnnotations() { __log.push("drawAnnotations"); }
let __chipLayoutCount = null;
function updatePenChip() {
  __log.push("updatePenChip");
  __chipLayoutCount = __s.pageLayouts.length;   // was it populated by now?
}
globalThis.__shouldFail = false;
async function renderSinglePage(pageNum, canvas, annotCanvas) {
  __log.push("renderSinglePage:" + pageNum);
  if (__shouldFail) throw new Error("boom");
  return { cssW: 600, cssH: 800, pdfW: 400 };
}
"""


def _run_render_page(state: dict, *, fail: bool = False) -> dict:
    src = VIEWER_JS.read_text(encoding="utf-8")
    fn = slice_source(src, "export async function renderPage",
                       "async function _rasterizePageImpl")
    script = "\n".join([
        RENDER_PAGE_FAKES,
        f"const __s = {json.dumps(state)};",
        "const getState = () => __s;",
        f"globalThis.__shouldFail = {json.dumps(fail)};",
        fn,
        """
await renderPage();
console.log(JSON.stringify({
  log: __log, chipLayoutCount: __chipLayoutCount, rendering: __s.rendering,
}));
""",
    ])
    return run_deno(script)


@pytest.mark.parametrize("displayMode, expectedLayouts", [("1up", 1), ("2up", 2)])
def test_render_page_refreshes_the_pen_chip_after_page_layouts_are_populated(
        displayMode, expectedLayouts):
    """REGRESSION for bug fix A: before the fix, the chip was never redrawn
    from renderPage at all, so it kept showing the scale from whenever it
    was last touched (startup, at scale 1, or the last colour/style pick)
    rather than the newly-shown page's actual px/pt ratio."""
    r = _run_render_page({
        "pdfDoc": {}, "rendering": False, "currentPage": 3, "totalPages": 10,
        "displayMode": displayMode, "pageLayouts": [],
        "scrollToBottomAfterRender": False,
    })
    assert "updatePenChip" in r["log"]
    # drawAnnotations, then the chip, in that order -- not before pageLayouts exist.
    assert r["log"].index("drawAnnotations") < r["log"].index("updatePenChip")
    assert r["chipLayoutCount"] == expectedLayouts
    assert r["rendering"] is False


# ---------------------------------------------------------------------------
# Bug fix B: the actual regression scenario -- a client from BEFORE the pen
# grid (ae85bfc) draws a translucent stroke saved by a client running the
# FIXED inkAnnotation/inkColor. Its drawInk ignores `opacity`/`widthPt`
# completely and passes `color`/`width` straight to the canvas, so the whole
# fix rides on the transparency already being baked into `color` as
# #rrggbbaa (which every canvas, old or new, honours) rather than carried
# in a separate field only new clients read.
# ---------------------------------------------------------------------------

# drawInk as shipped before the pen grid (ae85bfc, v2.15.1) -- the client a
# cached, not-yet-updated device still runs. Kept verbatim here rather than
# read from git history, so the suite runs outside a checkout too.
OLD_DRAW_INK = """
function drawInk(ctx, annot, w, h, rot) {
  const pts = annot.points;
  if (!pts || pts.length < 2) return;

  ctx.beginPath();
  const [x0, y0] = transformPt(pts[0][0], pts[0][1], w, h, rot);
  ctx.moveTo(x0, y0);
  for (let i = 1; i < pts.length; i++) {
    const [x, y] = transformPt(pts[i][0], pts[i][1], w, h, rot);
    ctx.lineTo(x, y);
  }
  ctx.strokeStyle = annot.color || "black";
  ctx.lineWidth = annot.width || 2;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.stroke();
}
"""


def test_an_old_clients_drawInk_draws_a_new_highlight_translucent_not_opaque():
    """The actual bug: before this fix, a highlight saved by a new client
    carried plain `color` (no alpha) plus an `opacity` field the OLD drawInk
    (predates widthPt/opacity entirely) never reads -- so it drew as an
    opaque bar over the music. After the fix, `color` itself carries the
    alpha, so even this old function -- unmodified, unaware `opacity` or
    `widthPt` exist -- draws it translucent."""
    pen = PEN_STYLE_JS.read_text(encoding="utf-8")
    new = ANNOTATIONS_JS.read_text(encoding="utf-8")
    state = _state()
    script = "\n".join([
        f'import {{ transformPt, cssPerPt }} from "{UTILS_JS.as_uri()}";',
        PRELUDE,
        f"const __s = {json.dumps(state)};",
        "const getState = () => __s;",
        slice_source(pen, "const PEN_WIDTHS_PT", "export function penStyleAt("),
        slice_source(new, "let _colorCtx", ";\n") + ";",
        _fn(pen, "penStyleAt"),
        _fn(new, "inkColor"),
        _fn(new, "inkCssWidth"),
        _fn(new, "inkAnnotation"),
        OLD_DRAW_INK,
        f"""
const layout = __s.pageLayouts[0];
const a = inkAnnotation([[0.1, 0.1], [0.2, 0.2]], "red", {{ row: 3, col: 2 }}, layout);
const ctx = recordingCtx();
drawInk(ctx, a, {W}, {H}, 0);
console.log(JSON.stringify({{ annot: a, stroke: ctx.st.strokes[0] }}));
""",
    ])
    r = run_deno(script)
    assert "opacity" not in r["annot"]                    # baked into colour, not a field
    assert r["annot"]["color"] == "#ff00004d"
    assert r["stroke"]["color"] == "#ff00004d"             # old client passes it straight through
    assert r["stroke"]["width"] == r["annot"]["width"] == 36   # 24pt highlight at 1.5 px/pt
    assert r["stroke"]["width"] > 0


@pytest.mark.parametrize("color, opacity, expected", [
    ("red", 1 / 255, "#ff000001"),      # single hex digit must be zero-padded
    ("red", 5 / 255, "#ff000005"),
    ("#123abc", 0.5, "#123abc80"),      # already #rrggbb: passed straight through by the canvas
])
def test_ink_colour_alpha_hex_is_two_digits_and_handles_already_normal_colours(
        color, opacity, expected):
    r = _run(_state(), f"console.log(JSON.stringify(inkColor({json.dumps(color)}, {opacity})));")
    assert r == expected
