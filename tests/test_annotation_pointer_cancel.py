"""A pointer the browser cancels mid-gesture (annotations.js onPointerCancel).

iOS sends pointercancel instead of pointerup when it takes the pointer away
(palm rejection, a system gesture). The gesture must still end:
- pen: the half-drawn stroke is discarded (not committed) and its preview
  wiped by a redraw;
- move: the mark stays where it was dropped and is saved, as at pointerup;
- eraser: the marks already removed are saved (see
  test_annotation_save_coalescing.py).

Runs the REAL pointer handlers under Deno via the gesture harness in
test_annotation_save_coalescing.py.
"""

from deno_harness import requires_deno
from test_annotation_save_coalescing import DOTS, _gesture

pytestmark = requires_deno

# The pen preview draws on the canvas directly; count its strokes.
CANVAS_2D = """
const window = { devicePixelRatio: 1 };
canvas.getContext = () => ({
  setTransform() {}, beginPath() {}, moveTo() {}, lineTo() {},
  stroke() { log.push("preview"); },
});
const inkCount = () => __s.annotations["0"].length;
"""

HALF_STROKE = CANVAS_2D + """
fire("pointerdown", 100, 100);
fire("pointermove", 120, 110);
fire("pointermove", 140, 120);
log.push("cancel");
fire("pointercancel", 140, 120);
log.push("after");
"""


def _after(log: list[str]) -> list[str]:
    return log[log.index("after") + 1:]


def test_a_cancelled_pen_stroke_is_not_committed():
    r = _gesture("pen", HALF_STROKE + 'log.push("marks:" + inkCount());')
    assert f"marks:{len(DOTS)}" in r["log"]
    assert "save" not in r["log"] and "undo" not in r["log"]


def test_a_cancelled_pen_stroke_is_wiped_from_the_screen():
    r = _gesture("pen", HALF_STROKE)
    log = r["log"]
    assert "draw" in log[log.index("cancel"):]


def test_hovering_after_a_cancelled_stroke_does_not_draw():
    """Regression: the stroke stayed live after pointercancel, so a hovering
    Pencil or mouse kept drawing preview ink with nothing pressed."""
    r = _gesture("pen", HALF_STROKE + """
fire("pointermove", 200, 200, 0);
fire("pointermove", 260, 240, 0);
""")
    assert "preview" not in _after(r["log"])


def test_the_next_stroke_after_a_cancel_is_committed_whole():
    r = _gesture("pen", HALF_STROKE + """
fire("pointerdown", 300, 100);
fire("pointermove", 320, 110);
fire("pointerup", 320, 110);
const s = __s.annotations["0"].at(-1);
log.push("marks:" + inkCount(), "points:" + s.points.length);
""")
    after = _after(r["log"])
    assert f"marks:{len(DOTS) + 1}" in after
    assert "points:2" in after                 # only the new stroke's points
    assert after.count("save") == 1


MOVE_THEN_CANCEL = """
const x0 = () => (__s.annotations["0"].find((a) => a.uuid === "d0").points[0][0] * 600).toFixed(1);
fire("pointerdown", 100, 400);                  // grab d0
fire("pointermove", 150, 400);
log.push("cancel");
fire("pointercancel", 150, 400);
log.push("after", "x:" + x0());
"""


def test_a_cancelled_move_keeps_the_mark_where_it_was_dropped_and_saves_it():
    """Regression: a cancelled move was never saved."""
    r = _gesture("move", MOVE_THEN_CANCEL)
    log = r["log"]
    assert "save" in log[log.index("cancel"):]
    assert "x:150.0" in log


def test_hovering_after_a_cancelled_move_does_not_drag_the_mark():
    """Regression: the drag stayed live after pointercancel, so the mark kept
    following a hovering pointer and jumped on the next press."""
    r = _gesture("move", MOVE_THEN_CANCEL + """
fire("pointermove", 300, 400, 0);
fire("pointerdown", 300, 700);                  // a press that misses every mark
fire("pointermove", 350, 700);
log.push("x:" + x0());
""")
    assert _after(r["log"])[-1] == "x:150.0"


def test_a_cancel_with_nothing_in_progress_does_nothing():
    for tool in ("pen", "move", "eraser"):
        r = _gesture(tool, CANVAS_2D + 'fire("pointercancel", 10, 10);')
        assert r["log"] == [], tool
        assert len(r["left"]) == len(DOTS)
