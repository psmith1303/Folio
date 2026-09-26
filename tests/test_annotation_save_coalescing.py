"""Annotation save coalescing and drag redraw throttling (Step 7).

Every save uploads the score's whole annotation set, so:
- saves requested while one is in flight collapse into a single queued save
  (which then sends the latest state), instead of one upload each;
- an eraser drag saves once, when the gesture ends (pointerup or
  pointercancel), instead of once per erased mark;
- move and eraser drags, and stamp images finishing loading, redraw at most
  once per animation frame.

The REAL functions are sliced out of annotations.js and run under Deno with
only the DOM, state and I/O stubbed; see deno_harness.py. Functions this
step added are sliced only if present, so each test fails on the pre-Step-7
code by the behaviour itself, not a missing marker.
"""

import json
from pathlib import Path

import pytest

from deno_harness import requires_deno, run_deno, slice_source
from test_annotation_tools import _const, _fn, _geometry

pytestmark = requires_deno

MODULES = Path(__file__).resolve().parent.parent / "web" / "static" / "modules"
ANNOTATIONS_JS = MODULES / "annotations.js"


def _src() -> str:
    return ANNOTATIONS_JS.read_text(encoding="utf-8")


def _optional(src: str, *names: str) -> str:
    """Slices of Step 7's helpers, or nothing where they don't exist."""
    out = []
    for n in names:
        if n.startswith("let "):
            decl = slice_source(src, n, ";\n") + ";\n" if n in src else ""
            out.append(decl)
        elif f"function {n}(" in src:
            out.append(_fn(src, n))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# saveAnnotations: saves queued behind an in-flight one coalesce
# ---------------------------------------------------------------------------

SAVE_PRELUDE = """
const __s = { currentScore: { filepath: "/m/A.pdf" }, annotations: { "0": [] } };
const getState = () => __s;
const puts = [];          // one entry per upload actually sent
const pending = [];       // resolvers for uploads still in flight
const _doSaveAnnotations = (s, force) => {
  puts.push({ path: s.currentScore.filepath, n: s.annotations["0"].length, force });
  return new Promise((r) => pending.push(r));
};
const tick = () => new Promise((r) => setTimeout(r, 0));
const finishUpload = async () => { pending.shift()(); await tick(); };
const edit = () => __s.annotations["0"].push({});
"""


def _save(body: str):
    src = _src()
    chain = slice_source(src, "let _saveChain = ", "\nasync function _doSaveAnnotations")
    return run_deno(f"""{SAVE_PRELUDE}
{chain}
{body}
await tick();
while (pending.length) await finishUpload();        // let every queued upload run
console.log(JSON.stringify(puts));
""")


def test_edits_during_an_upload_send_one_more_upload_with_the_latest_state():
    """Regression: every edit queued its own full upload, so a burst of
    edits during a slow upload sent one PUT per edit."""
    puts = _save("""
edit(); saveAnnotations(); await tick();            // upload 1 in flight
for (let i = 0; i < 9; i++) { edit(); saveAnnotations(); }
await finishUpload();
""")
    assert puts == [
        {"path": "/m/A.pdf", "n": 1, "force": False},
        {"path": "/m/A.pdf", "n": 10, "force": False},
    ]


def test_an_edit_after_the_queued_upload_starts_is_not_lost():
    puts = _save("""
edit(); saveAnnotations(); await tick();
edit(); saveAnnotations();
await finishUpload();                               // upload 2 now in flight
edit(); saveAnnotations();
await finishUpload();
""")
    assert [p["n"] for p in puts] == [1, 2, 3]


@pytest.mark.parametrize("forces", [[False, True], [True, False], [True, True]])
def test_a_forced_save_keeps_its_force_when_coalesced(forces):
    """saveAnnotations(true) is the "overwrite" answer to a save conflict; it
    must not be downgraded by riding along with an ordinary save."""
    a, b = (json.dumps(f) for f in forces)
    puts = _save(f"""
saveAnnotations(); await tick();
edit(); saveAnnotations({a}); edit(); saveAnnotations({b});
await finishUpload();
""")
    assert [p["force"] for p in puts] == [False, True]


def test_switching_score_does_not_fold_the_new_scores_save_into_the_old_one():
    """A queued save for score A is dropped once B is open; B's own save must
    still go out rather than being merged into A's."""
    puts = _save("""
saveAnnotations(); await tick();                    // A in flight
edit(); saveAnnotations();                          // A queued
__s.currentScore = { filepath: "/m/B.pdf" };
edit(); saveAnnotations();                          // B
await finishUpload();
""")
    assert [p["path"] for p in puts] == ["/m/A.pdf", "/m/B.pdf"]


# ---------------------------------------------------------------------------
# Eraser and move gestures, through the real pointer handlers
# ---------------------------------------------------------------------------

GESTURE_PRELUDE = """
const __s = %(state)s;
const getState = () => __s;
const log = [];
const saveAnnotations = () => log.push("save");
const drawAnnotations = () => log.push("draw");
// The live pen stroke is the page redrawn with it; log each redraw.
const drawPageAnnotations = () => log.push("preview");
// A committed stroke's fields are covered by test_pen_style.py.
const inkAnnotation = (points) => ({ uuid: crypto.randomUUID(), type: "ink", points });
const pushUndo = (pg) => log.push("undo");
const frames = [];
const requestAnimationFrame = (cb) => frames.push(cb);
const frame = () => { log.push("frame"); frames.splice(0).forEach((cb) => cb()); };
const setTool = () => {}, handleTextClick = () => {};
const stampToolCursor = () => "", startPageToolCursor = () => "";
const placeStamp = () => false, placeStartPage = () => false;
const nextPage = () => {}, prevPage = () => {};
const listeners = {};
const canvas = {
  getBoundingClientRect: () => ({ left: 0, top: 0 }),
  setPointerCapture() {},
  addEventListener: (ev, fn) => { listeners[ev] = fn; },
};
const ev = (x, y, buttons = 1) =>
  ({ clientX: x, clientY: y, buttons, pointerType: "pen", pointerId: 1, preventDefault() {} });
const fire = (name, x, y, buttons) => listeners[name] && listeners[name](ev(x, y, buttons));
"""

GESTURE_FNS = ["canvasCoords", "pagePoint", "findTopHit", "isPlacementTool",
               "onPointerDown", "onPointerMove", "onPointerUp", "eraseAt",
               "startMove", "moveTo", "endMove", "setupAnnotCanvas"]


def _ink_at(x: float, y: float, uid: str) -> dict:
    return {"uuid": uid, "type": "ink", "points": [[x, y]], "color": "red", "width": 3}


# Five dots along y = 400 on a 600x800 page, 100 px apart.
DOTS = [_ink_at((100 + 100 * i) / 600, 0.5, f"d{i}") for i in range(5)]


def _gesture(tool: str, body: str) -> dict:
    src = _src()
    state = {"activeTool": tool, "displayMode": "fit", "pencilOnly": False,
             "pageLayouts": [{"page": 1, "cssW": 600, "cssH": 800, "pdfW": 612}],
             "rotations": {}, "annotations": {"0": DOTS}, "currentStroke": [],
             "draggingAnnot": None, "penColor": "red"}
    return run_deno("\n".join([
        _geometry(src),
        GESTURE_PRELUDE % {"state": json.dumps(state)},
        _const(src, "PLACEMENT_TOOLS"),
        _optional(src, "let _drawScheduled", "scheduleDraw",
                  "let _eraserUnsaved", "flushEraserSave", "onPointerCancel"),
        *(_fn(src, n) for n in GESTURE_FNS),
        "setupAnnotCanvas(canvas, 0);",
        body,
        "console.log(JSON.stringify({ log, left: __s.annotations['0'].map((a) => a.uuid) }));",
    ]))


SWIPE = """
fire("pointerdown", 100, 400);
for (const x of [200, 300, 400, 500]) fire("pointermove", x, 400);
"""


def test_an_eraser_swipe_saves_once_when_it_ends():
    """Regression: each erased mark saved (uploaded) the whole score."""
    r = _gesture("eraser", SWIPE + 'log.push("lift"); fire("pointerup", 500, 400);')
    assert r["left"] == []
    assert r["log"].count("save") == 1
    assert r["log"].index("save") > r["log"].index("lift")


def test_a_cancelled_eraser_swipe_still_saves_what_it_erased():
    """iOS cancels the pointer (e.g. palm rejection) instead of lifting it;
    the marks are already gone from the screen and must be saved."""
    r = _gesture("eraser", SWIPE + 'fire("pointercancel", 500, 400);')
    assert r["left"] == []
    assert r["log"].count("save") == 1


def test_an_eraser_swipe_over_nothing_does_not_save():
    r = _gesture("eraser", """
fire("pointerdown", 100, 700);
fire("pointermove", 300, 700);
fire("pointerup", 300, 700);
""")
    assert len(r["left"]) == 5
    assert "save" not in r["log"]


def test_an_eraser_save_is_not_repeated_by_the_next_gesture():
    r = _gesture("eraser", SWIPE + """
fire("pointerup", 500, 400);
fire("pointerdown", 100, 700); fire("pointerup", 100, 700);
fire("pointercancel", 100, 700);
""")
    assert r["log"].count("save") == 1


def test_an_eraser_swipe_redraws_once_per_frame():
    """Regression: every erased mark redrew both pages synchronously."""
    r = _gesture("eraser", SWIPE + "frame();")
    log = r["log"]
    assert log.count("draw") == 1
    assert log.index("draw") > log.index("frame")


def test_a_move_drag_redraws_once_per_frame_and_saves_once():
    """Regression: every pointermove of a drag redrew both pages."""
    r = _gesture("move", """
fire("pointerdown", 100, 400);
for (const x of [110, 120, 130]) fire("pointermove", x, 400);
frame();
for (const x of [140, 150]) fire("pointermove", x, 400);
frame();
fire("pointerup", 150, 400);
""")
    log = r["log"]
    assert log.count("undo") == 1
    # one draw per frame, then endMove's final paint
    assert log == ["undo", "frame", "draw", "frame", "draw", "save", "draw"]


def test_no_frame_is_requested_while_one_is_pending():
    r = _gesture("move", """
fire("pointerdown", 100, 400);
for (let x = 101; x < 150; x++) fire("pointermove", x, 400);
log.push("frames:" + frames.length);
""")
    assert "frames:1" in r["log"]


# ---------------------------------------------------------------------------
# Stamp images: load callbacks coalesce into one redraw
# ---------------------------------------------------------------------------

def test_stamps_finishing_loading_together_redraw_once():
    """Each redraw before a stamp's image loads registers another onReady
    callback; when the loads land they must cost one redraw, not one each."""
    src = _src()
    r = run_deno("\n".join([
        _geometry(src, real=("drawStamp",)),
        """
const log = [];
const drawAnnotations = () => log.push("draw");
const frames = [];
const requestAnimationFrame = (cb) => frames.push(cb);
const onReadies = [];
const getStampImage = (id, color, onReady) => { onReadies.push(onReady); return null; };
""",
        _optional(src, "let _drawScheduled", "scheduleDraw"),
        _fn(src, "drawStamp"),
        """
for (let i = 0; i < 4; i++) drawStamp(null, { id: "f", x: 0.5, y: 0.5, size: 5 }, 600, 800, 0, 612);
onReadies.forEach((cb) => cb());
log.push("frame");
frames.splice(0).forEach((cb) => cb());
console.log(JSON.stringify(log));
""",
    ]))
    assert r == ["frame", "draw"]
