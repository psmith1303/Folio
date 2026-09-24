"""Parity tests for client-side library filtering.

Since 2.9.5 the library view filters, sorts and derives facets in JavaScript
instead of asking the server to. That is what makes filtering work offline:
the service worker caches API responses under the *exact* request URL, so a
server-filtered view was only available offline if that precise query string
had been fetched while online. Since 2.13.0 /api/library is a plain list and
the rules live only in web/static/modules/library-filter.js, which the library
view and the setlist song picker share.

These tests guard:

* source-level invariants that make offline filtering possible at all
* the real library-filter.js against the old endpoint's outputs, recorded in
  tests/data/library_filter_golden.json before the server stopped filtering
"""

import json
import re
from pathlib import Path

import pytest

from deno_harness import requires_deno, run_deno
from web.core import Score

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
LIBRARY_JS = STATIC / "modules" / "library.js"
CACHE_JS = STATIC / "modules" / "cache.js"
FILTER_JS = STATIC / "modules" / "library-filter.js"
GOLDEN = Path(__file__).resolve().parent / "data" / "library_filter_golden.json"


def _api_url_in(source: str, func: str) -> str:
    """Return the URL literal `func` passes to api()/fetch().

    Handles backtick template literals as well as quoted strings, so a
    reintroduced `api(`/api/library?${params}`)` is reported as the query-string
    regression it is rather than failing to parse.
    """
    start = source.index(func)
    body = source[start : source.index("\n}", start)]
    match = re.search(r"""(?:api|fetch)\(\s*([`"'])(.*?)\1""", body, re.S)
    if match is None:
        raise AssertionError(f"no api()/fetch() call found in {func}")
    return match.group(2)


class TestOfflineFilterInvariants:
    """Hermetic guards for the shape that makes offline filtering possible."""

    def test_load_library_requests_no_query_parameters(self):
        """A per-filter URL is a per-filter cache key, so offline it misses."""
        url = _api_url_in(LIBRARY_JS.read_text(encoding="utf-8"),
                          "export async function loadLibrary")
        assert "?" not in url, (
            f"loadLibrary requests {url!r}. A query string makes the offline cache "
            "key filter-specific, so a search never used while online returns 503 "
            "and the library list silently stops responding."
        )

    def test_cache_library_primes_the_url_the_app_requests(self):
        """'Refresh Library Cache' must prime the key loadLibrary will ask for."""
        requested = _api_url_in(LIBRARY_JS.read_text(encoding="utf-8"),
                                "export async function loadLibrary")
        primed = _api_url_in(CACHE_JS.read_text(encoding="utf-8"),
                             "export async function cacheLibrary")
        assert primed == requested, (
            f"cacheLibrary primes {primed!r} but loadLibrary requests "
            f"{requested!r}; the offline prep caches a key the app never uses."
        )


def _score(filename, folder_tags=None):
    """Score derives composer/title/tags from the filename convention:
    ``Composer - Title -- tag1 tag2.pdf``."""
    return Score(f"/m/{filename}", filename, folder_tags)


# Deliberately includes mixed case in composer and title (the endpoint
# lowercases for both matching and sorting), scores sharing composer+tags to
# create ties under the tags sort, tag subsets, an untagged score, a folder-tag
# source as well as filename tags, and scores that match a query by composer
# only or by title only.
LIBRARY = [
    _score("Bach, J.S. - Suite No. 1 -- cello baroque.pdf"),
    _score("Bach, J.S. - Suite No. 2 -- cello baroque.pdf"),
    _score("bach, c.p.e. - SONATA -- baroque.pdf"),
    _score("Mozart - Horn Concerto -- horn classical.pdf"),
    _score("Mozart - Bach Variations -- classical.pdf"),
    _score("Zelenka - Trio.pdf"),
    _score("Mozart - Aria.pdf", {"classical"}),
    _score("Mozart - Zzz -- classical.pdf"),
]

CASES = [
    {"q": q, "composer": c, "tags": t, "sort": s, "desc": d}
    for q in ["", "bach", "SUITE", "zzz", "nomatch"]
    for c in ["", "Mozart"]
    for t in [[], ["classical"], ["cello", "baroque"]]
    for s in ["composer", "title", "tags"]
    for d in [False, True]
]

# Recorded once from the server's get_library() (see the file's
# "recorded_from"); it can't be regenerated, because the server no longer
# filters. The library and cases above are the ones it was recorded with.
GOLDEN_DATA = json.loads(GOLDEN.read_text(encoding="utf-8"))
RECORDED = GOLDEN_DATA["views"]


def test_golden_matches_the_library_and_cases():
    """Editing LIBRARY or CASES would silently invalidate the recording."""
    assert GOLDEN_DATA["library"] == [s.to_dict() for s in LIBRARY]
    assert [v["case"] for v in RECORDED] == CASES


@pytest.fixture(scope="module")
def js_results():
    """Run the real filterLibrary() over every case in a single deno process."""
    return run_deno(f"""
import {{ filterLibrary }} from "{FILTER_JS.as_uri()}";
const all = {json.dumps(GOLDEN_DATA["library"])};
console.log(JSON.stringify({json.dumps(CASES)}.map((c) => {{
  const v = filterLibrary(all, c);
  return {{ scores: v.scores.map((s) => s.filepath), composers: v.composers, tags: v.tags }};
}})));
""", timeout=180)


@requires_deno
@pytest.mark.parametrize("idx", range(len(CASES)), ids=[
    "q={},comp={},tags={},{}{}".format(
        c["q"] or "-", c["composer"] or "-", "+".join(c["tags"]) or "-",
        c["sort"], "-desc" if c["desc"] else "")
    for c in CASES
])
class TestFilterParity:
    def test_same_scores_selected(self, idx, js_results):
        assert set(js_results[idx]["scores"]) == set(RECORDED[idx]["scores"])

    def test_same_facets(self, idx, js_results):
        assert js_results[idx]["composers"] == RECORDED[idx]["composers"]
        assert js_results[idx]["tags"] == RECORDED[idx]["tags"]

    def test_same_order(self, idx, js_results):
        """Order must match exactly, except that the JS breaks tags-sort ties.

        The endpoint's tags key was (sorted(tags), composer), leaving
        same-composer/same-tag scores tied and ordered by scan order. The JS
        adds title as a third key so the ordering is total and independent of
        how the fetched list arrived. Where they differ, the *sequence of sort
        keys* must still be identical — only exact ties may move.
        """
        case = CASES[idx]
        expected = RECORDED[idx]["scores"]
        got = js_results[idx]["scores"]
        if got == expected:
            return
        assert case["sort"] == "tags", (
            f"order diverged on sort={case['sort']}, where no divergence is sanctioned"
        )
        key = {s["filepath"]: (tuple(s["tags"]), s["composer"].lower())
               for s in GOLDEN_DATA["library"]}
        assert [key[p] for p in got] == [key[p] for p in expected], (
            "rows crossed a sort boundary; only exact ties may be reordered"
        )


# ---------------------------------------------------------------------------
# searchScores — the setlist song picker's search
# ---------------------------------------------------------------------------


def _search(queries: list[str], library=None) -> list[list[str]]:
    return run_deno(f"""
import {{ searchScores }} from "{FILTER_JS.as_uri()}";
const all = {json.dumps(GOLDEN_DATA["library"] if library is None else library)};
console.log(JSON.stringify({json.dumps(queries)}.map(
  (q) => searchScores(all, q).map((s) => s.filepath))));
""")


def _recorded(q: str) -> list[str]:
    """The old endpoint's /api/library?q=<q> (default composer sort)."""
    case = {"q": q, "composer": "", "tags": [], "sort": "composer", "desc": False}
    return next(v["scores"] for v in RECORDED if v["case"] == case)


@requires_deno
def test_search_matches_the_old_endpoint():
    """Regression: the song picker fetched /api/library?q=... per keystroke,
    which failed offline. It now searches the in-memory library and must
    give what the endpoint gave."""
    queries = ["", "bach", "SUITE", "zzz", "nomatch"]
    assert _search(queries) == [_recorded(q) for q in queries]


@requires_deno
def test_search_trims_and_ignores_case():
    padded, upper = _search(["  bach ", "BACH"])
    assert padded == upper == _recorded("bach")


@requires_deno
def test_search_of_an_empty_library_is_empty():
    assert _search(["bach", ""], library=[]) == [[], []]
