"""esc() must make any text safe inside HTML content AND quoted attributes.

The list views put composer, title and tags into both a cell and its
title="..." tooltip. esc() used to round-trip the text through a DOM
element, which escapes & < > but not quotes, so a double quote in a name
ended the attribute early: the tooltip was cut short and the rest of the
name spilled into the tag as junk attributes.

Runs the REAL utils.js esc() under Deno and parses the markup it builds
with Python's HTML parser, as a browser would.
"""

import json
from html.parser import HTMLParser

import pytest

from deno_harness import requires_deno
from js_module_harness import MODULES, run_module

pytestmark = requires_deno

# The old esc() needs a DOM. This stub serializes a text node the way the
# HTML spec says browsers do (escape &, <, >, and U+00A0; never quotes).
SPEC_TEXT_DOM = r"""
globalThis.document = {
  createElement: () => {
    let text = "";
    return {
      set textContent(v) { text = v === null ? "" : String(v); },
      get innerHTML() {
        return text.replace(/&/g, "&amp;").replace(/ /g, "&nbsp;")
          .replace(/</g, "&lt;").replace(/>/g, "&gt;");
      },
    };
  },
};
"""

NAMES = [
    'Bach "the elder"',
    "O'Neill",
    "Rock & Roll",
    "<b>not bold</b>",
    'He said "it\'s" <fine> & done',
    "naïve café — Dvořák",
    "no break",
    "plain",
    "",
]


class _Cells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag == "td":
            self.cells.append({"attrs": dict(attrs), "text": ""})

    def handle_data(self, data):
        if self.cells:
            self.cells[-1]["text"] += data


def _cells(names: list[str]) -> list[dict]:
    html = run_module(MODULES / "utils.js", [], f"""
const names = {json.dumps(names)};
console.log(JSON.stringify(names.map((n) => `<td title="${{M.esc(n)}}">${{M.esc(n)}}</td>`).join("")));
""", setup=SPEC_TEXT_DOM)
    parser = _Cells()
    parser.feed(html)
    return parser.cells


@pytest.mark.parametrize("name", NAMES)
def test_text_survives_a_quoted_attribute_and_cell(name):
    """Regression: a double quote in a composer or title cut its tooltip
    short and leaked the rest into the tag as bogus attributes."""
    [cell] = _cells([name])
    assert cell == {"attrs": {"title": name}, "text": name}


def test_many_values_in_one_row_stay_separate():
    cells = _cells(NAMES)
    assert [c["attrs"] for c in cells] == [{"title": n} for n in NAMES]
    assert [c["text"] for c in cells] == NAMES


@pytest.mark.parametrize("value, expected", [(None, ""), (0, "0"), (42, "42")])
def test_non_string_values(value, expected):
    got = run_module(MODULES / "utils.js", [],
                     f"console.log(JSON.stringify(M.esc({json.dumps(value)})));",
                     setup=SPEC_TEXT_DOM)
    assert got == expected
