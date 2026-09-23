"""Parity tests for client-side library filtering.

Since 2.9.5 the library view filters, sorts and derives facets in JavaScript
(`applyFilters` in web/static/modules/library.js) instead of asking the server
to. That is what makes filtering work offline: the service worker caches API
responses under the *exact* request URL, so a server-filtered view was only
available offline if that precise query string had been fetched while online.
Anything else got a 503 and left the table showing its previous rows.

The cost is the same filtering logic in two languages with no shared source —
the drift hazard these tests exist to contain. They guard both halves:

* source-level invariants that make offline filtering possible at all
* an executable parity check running the real JS against the real endpoint
"""

import json
import re
from pathlib import Path

import pytest

import web.server as srv
from deno_harness import requires_deno, run_deno, slice_source
from web.core import Score

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
LIBRARY_JS = STATIC / "modules" / "library.js"
CACHE_JS = STATIC / "modules" / "cache.js"


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

# Stubs stand in for the DOM and the render calls; everything between them is
# the real, unmodified source sliced out of library.js.
HARNESS = """
let __state;
const getState = () => __state;
const searchInput = { value: "" };
const composerFilter = { value: "" };
const libraryStatus = { textContent: "" };
const renderLibrary = () => {};
const renderComposerFilter = () => {};
const renderTags = () => {};
const CACHE_AVAILABLE = false;
const refreshCacheStatus = () => {};

%(core)s

const ALL = %(all)s;
const out = [];
for (const c of %(cases)s) {
  __state = {
    allScores: ALL, scores: [], composers: [], tags: [],
    selectedTags: new Set(c.tags), sortCol: c.sort, sortDesc: c.desc,
  };
  searchInput.value = c.q;
  composerFilter.value = c.composer;
  applyFilters();
  out.push({
    scores: __state.scores.map((s) => s.filepath),
    composers: __state.composers,
    tags: __state.tags,
  });
}
console.log(JSON.stringify(out));
"""


def _extract_core(source: str) -> str:
    return slice_source(source, "const cmpStr =", "function renderLibrary()")


@pytest.fixture(scope="module")
def js_results():
    """Run the real applyFilters() over every case in a single deno process."""
    script = HARNESS % {
        "core": _extract_core(LIBRARY_JS.read_text(encoding="utf-8")),
        "all": json.dumps([s.to_dict() for s in LIBRARY]),
        "cases": json.dumps(CASES),
    }
    return run_deno(script, timeout=180)


def _server_view(case, monkeypatch):
    monkeypatch.setattr(srv.state, "scores", LIBRARY)
    return srv.get_library(q=case["q"], composer=case["composer"],
                           tag=list(case["tags"]), sort=case["sort"],
                           desc=case["desc"])


@requires_deno
@pytest.mark.parametrize("idx", range(len(CASES)), ids=[
    "q={},comp={},tags={},{}{}".format(
        c["q"] or "-", c["composer"] or "-", "+".join(c["tags"]) or "-",
        c["sort"], "-desc" if c["desc"] else "")
    for c in CASES
])
class TestFilterParity:
    def test_same_scores_selected(self, idx, js_results, monkeypatch):
        expected = {s["filepath"]
                    for s in _server_view(CASES[idx], monkeypatch)["scores"]}
        assert set(js_results[idx]["scores"]) == expected

    def test_same_facets(self, idx, js_results, monkeypatch):
        view = _server_view(CASES[idx], monkeypatch)
        assert js_results[idx]["composers"] == view["composers"]
        assert js_results[idx]["tags"] == view["tags"]

    def test_same_order(self, idx, js_results, monkeypatch):
        """Order must match exactly, except that the JS breaks tags-sort ties.

        The endpoint's tags key is (sorted(tags), composer), leaving
        same-composer/same-tag scores tied and ordered by scan order. The JS
        adds title as a third key so the ordering is total and independent of
        how the fetched list arrived. Where they differ, the *sequence of sort
        keys* must still be identical — only exact ties may move.
        """
        case = CASES[idx]
        scores = _server_view(case, monkeypatch)["scores"]
        expected = [s["filepath"] for s in scores]
        got = js_results[idx]["scores"]
        if got == expected:
            return
        assert case["sort"] == "tags", (
            f"order diverged on sort={case['sort']}, where no divergence is sanctioned"
        )
        key = {s["filepath"]: (tuple(s["tags"]), s["composer"].lower())
               for s in scores}
        assert [key[p] for p in got] == [key[p] for p in expected], (
            "rows crossed a sort boundary; only exact ties may be reordered"
        )
