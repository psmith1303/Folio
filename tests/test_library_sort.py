"""Library sort order details the golden parity data doesn't exercise.

tests/test_library_filter_parity.py checks filterLibrary against orderings
recorded from the old server, but its small library has no exact ties and
no tag lists that differ element-wise from their string form, and it lets
tags-sort ties move. These pin those cases, so the sort keys computed once
per score (Step 9) keep ordering exactly as the per-comparison sort did:

- ties keep their input order, ascending and descending (a stable sort,
  not a reversed one);
- tag lists compare element by element, shorter first on a common prefix,
  like Python lists;
- the tags sort breaks composer ties on title.

Runs the REAL library-filter.js under Deno.
"""

import json

import pytest

from deno_harness import requires_deno
from js_module_harness import MODULES, run_module

pytestmark = requires_deno


def _order(scores: list[dict], sort: str, desc: bool = False) -> list[str]:
    return run_module(MODULES / "library-filter.js", [], f"""
const v = M.filterLibrary({json.dumps(scores)}, {{ sort: "{sort}", desc: {json.dumps(desc)} }});
console.log(JSON.stringify(v.scores.map((s) => s.filepath)));
""")


def _score(path: str, composer: str = "Bach", title: str = "Suite", tags: list | None = None) -> dict:
    return {"filepath": path, "composer": composer, "title": title, "tags": tags or []}


# Same composer and title, different files (e.g. two editions).
TWINS = [_score("/b/second.pdf"), _score("/a/first.pdf"), _score("/c/third.pdf")]


@pytest.mark.parametrize("sort", ["composer", "title", "tags"])
@pytest.mark.parametrize("desc", [False, True])
def test_exact_ties_keep_their_input_order(sort, desc):
    assert _order(TWINS, sort, desc) == [s["filepath"] for s in TWINS]


def test_descending_reverses_distinct_keys_but_not_ties():
    scores = [_score("/1", title="A"), _score("/2", title="B"), _score("/3", title="B"), _score("/4", title="C")]
    assert _order(scores, "title", desc=True) == ["/4", "/2", "/3", "/1"]


def test_tag_lists_compare_element_by_element():
    """["a b"] sorts after ["a", "c"]: element-wise "a" < "a b". As joined
    strings it would be the other way round ("a b" < "a,c")."""
    scores = [_score("/space", tags=["a b"]), _score("/pair", tags=["a", "c"])]
    assert _order(scores, "tags") == ["/pair", "/space"]


def test_a_shorter_tag_list_sorts_first_on_a_common_prefix():
    scores = [_score("/ab", tags=["a", "b"]), _score("/none"), _score("/a", tags=["a"])]
    assert _order(scores, "tags") == ["/none", "/a", "/ab"]


def test_tags_are_compared_sorted_within_each_score():
    scores = [_score("/zb", tags=["z", "b"]), _score("/c", tags=["c"])]
    assert _order(scores, "tags") == ["/zb", "/c"]      # ["b", "z"] < ["c"]


def test_tags_sort_breaks_composer_ties_on_title():
    scores = [_score("/y", title="Zeta", tags=["x"]), _score("/x", title="alpha", tags=["x"])]
    assert _order(scores, "tags") == ["/x", "/y"]


@pytest.mark.parametrize("sort, expected", [
    ("composer", ["/aB", "/ba", "/Bb"]),     # (composer, title), case-insensitive
    ("title", ["/ba", "/aB", "/Bb"]),        # (title, composer)
])
def test_keys_are_case_insensitive_and_ordered(sort, expected):
    scores = [_score("/Bb", "b", "B"), _score("/aB", "A", "b"), _score("/ba", "b", "a")]
    assert _order(scores, sort) == expected


def test_unknown_sort_keeps_the_given_order():
    scores = [_score("/2", "Z"), _score("/1", "A")]
    assert _order(scores, "toString") == ["/2", "/1"]
    assert _order(scores, "bogus") == ["/2", "/1"]
