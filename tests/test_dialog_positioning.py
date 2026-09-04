"""Regression tests for the v2.9.4 modal-dialog hit-test fix (commit d42bd20),
which shipped with no test coverage.

showModal() promotes a <dialog> to the browser's top layer, where the UA
already centres it (position:fixed; inset:0; margin:auto). app.css was also
centring it by hand with translate(-50%, -50%), which in WebKit desyncs the
painted box from the hit-test box: the dialog draws where you see it, but taps
resolve against the untransformed position. On iPad this put the whole button
row off by about one button — tapping Close in the offline dialog fired Clear
PDF Cache instead, so the dialog never closed and looked like it was
reopening itself.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"

# The declarations that must never appear on a modal <dialog>.
POSITIONING_PROPS = {"position", "top", "left", "right", "bottom", "transform"}

# The pre-fix rule, kept verbatim so the parsing helper is proven against the
# exact text that caused the bug rather than a hand-made approximation.
PRE_FIX_DIALOG_RULE = """
dialog {
  background: var(--bg-panel);
  min-width: 400px;
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 1000;
  max-width: 90vw;
}
"""


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def rule_blocks(css: str, selector: str) -> list[str]:
    """Return EVERY declaration body whose selector list contains `selector`
    as a standalone token — exactly `dialog { ... }`, `dialog, .x { ... }` and
    `.x, dialog { ... }` alike, but not `dialog:not([open])`,
    `dialog.dialog-polyfilled`, `dialog::backdrop` or `dialog h3`.

    `selector` may be preceded by the end of a previous rule (`}`), a comma
    from an earlier entry in the same selector list, or the start of the
    file; it must be followed (after optional whitespace) by a comma or `{`
    — anything else (`.`, `:`, another selector via a descendant combinator)
    means it is not a standalone token and is excluded.

    All matches, not just the first: a second `dialog { position: fixed; ... }`
    later in the file — inside an @media block, or folded into a grouped
    selector list — would reintroduce the tap misrouting while an earlier,
    clean rule still looked fine.
    """
    pattern = re.compile(
        r"(?:^|[};,])\s*" + re.escape(selector) + r"\s*(?=[,{])[^{}]*\{([^{}]*)\}",
        re.M,
    )
    return [m.group(1) for m in pattern.finditer(_strip_comments(css))]


def rule_block(css: str, selector: str) -> str | None:
    blocks = rule_blocks(css, selector)
    return blocks[0] if blocks else None


def declared_properties(block: str) -> set[str]:
    props = set()
    for decl in block.split(";"):
        if ":" in decl:
            props.add(decl.split(":", 1)[0].strip().lower())
    return props


@pytest.fixture(scope="module")
def app_css() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


class TestModalDialogPositioning:
    def test_helper_detects_the_original_bug(self):
        """Certifies the check: the pre-fix rule must trip every guard below."""
        block = rule_block(PRE_FIX_DIALOG_RULE, "dialog")
        assert block is not None
        offenders = declared_properties(block) & POSITIONING_PROPS
        assert offenders == {"position", "top", "left", "transform"}

    def test_a_later_rule_cannot_hide_behind_an_earlier_clean_one(self):
        """The .search() hole: only the first matching rule used to be checked."""
        css = """
dialog {
  padding: 20px;
}

@media (max-width: 600px) {
dialog {
  position: fixed;
  transform: translate(-50%, -50%);
}
}
"""
        blocks = rule_blocks(css, "dialog")
        assert len(blocks) == 2
        declared = set().union(*(declared_properties(b) for b in blocks))
        assert declared & POSITIONING_PROPS == {"position", "transform"}

    @pytest.mark.parametrize("css", [
        ".modal, dialog {\n  position: fixed;\n  transform: translate(-50%, -50%);\n}",
        "dialog, .modal {\n  position: fixed;\n  transform: translate(-50%, -50%);\n}",
    ], ids=["dialog-second-in-group", "dialog-first-in-group"])
    def test_grouped_selector_cannot_hide_the_bug_either(self, css):
        """A `dialog` folded into a grouped selector list must still be seen."""
        blocks = rule_blocks(css, "dialog")
        assert len(blocks) == 1
        assert declared_properties(blocks[0]) & POSITIONING_PROPS == {
            "position", "transform",
        }

    @pytest.mark.parametrize("css", [
        "dialog.dialog-polyfilled {\n  position: fixed;\n}",
        "dialog:not([open]) {\n  display: none;\n}",
        "dialog::backdrop {\n  background: black;\n}",
        "dialog h3 {\n  margin-bottom: 8px;\n}",
    ], ids=["compound-class", "pseudo-class", "pseudo-element", "descendant"])
    def test_non_bare_dialog_selectors_are_not_matched(self, css):
        """These must stay excluded, or the guard would flag rules it should not."""
        assert rule_blocks(css, "dialog") == []

    def test_bare_dialog_declares_no_manual_positioning(self, app_css):
        blocks = rule_blocks(app_css, "dialog")
        assert blocks, "no bare `dialog` rule found in app.css"
        declared = set().union(*(declared_properties(b) for b in blocks))
        offenders = sorted(declared & POSITIONING_PROPS)
        assert not offenders, (
            f"bare `dialog` rule re-declares {offenders}; this desyncs paint from "
            "hit testing in WebKit and mis-routes taps to the neighbouring control. "
            "Manual centring belongs on dialog.dialog-polyfilled only."
        )

    def test_polyfill_rule_keeps_manual_positioning(self, app_css):
        """The pre-15.4 Safari fallback renders in normal flow and does need it."""
        block = rule_block(app_css, "dialog.dialog-polyfilled")
        assert block is not None, "dialog.dialog-polyfilled rule is missing"
        props = declared_properties(block)
        assert {"position", "top", "left", "transform"} <= props

    def test_polyfill_class_is_applied_by_the_js(self, app_js):
        """Guards the other half: CSS and JS must not drift apart."""
        assert 'classList.add("dialog-polyfilled")' in app_js

    @pytest.mark.parametrize("selector", ["dialog", "dialog.dialog-polyfilled"])
    def test_selectors_resolve_to_distinct_rules(self, app_css, selector):
        assert rule_block(app_css, selector) is not None
