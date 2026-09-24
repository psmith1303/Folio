"""The Library, Recent and Newest tables and their cache buttons (Step 9).

Rows are built by score-table.js as one HTML string, and each table has a
single delegated click listener: a click on a row's cache button toggles
that score's offline copy, a click anywhere else in the row opens it. The
cache button's markup comes from cache.js (cacheButtonHtml) in the same
state refreshCacheStatus gives it; clicks on it are handled by
cache.onCacheButtonClick, which the setlist songs table uses too.

Runs the REAL score-table.js / cache.js / recent.js / newest.js under Deno,
with the viewer, API and DOM stubbed (js_module_harness.py) and in-memory
fakes for IndexedDB and the Cache API; rendered HTML is parsed in Python.
"""

import json
from html.parser import HTMLParser

import pytest

from deno_harness import requires_deno
from js_module_harness import MODULES, run_module

pytestmark = requires_deno

# Secure-context globals cache.js reads when it loads, and in-memory
# IndexedDB ("folio-lru" entries) and Cache Storage that record what's done.
FAKE_STORAGE = r"""
globalThis.window = globalThis;
globalThis.isSecureContext = true;
globalThis.__lru = new Map();
const __done = (req, result) => setTimeout(() => { req.result = result; req.onsuccess?.(); }, 0);
const __tx = () => {
  const tx = {
    objectStore: () => ({
      getAll: () => { const r = {}; __done(r, [...__lru.values()]); return r; },
      get: (k) => { const r = {}; __done(r, __lru.get(k)); return r; },
      put: (v) => { __lru.set(v.path, v); },
      delete: (k) => { __lru.delete(k); },
      clear: () => __lru.clear(),
    }),
  };
  setTimeout(() => tx.oncomplete?.(), 5);
  return tx;
};
globalThis.indexedDB = {
  open: () => { const r = {}; __done(r, { transaction: __tx, objectStoreNames: { contains: () => true } }); return r; },
};
Object.defineProperty(globalThis, "caches", { configurable: true, value: {
  open: async () => ({
    match: async (k) => { __log.push("cache.match:" + k); return __lru.has(decodeURIComponent(k.split("path=")[1])) ? {} : undefined; },
    put: async (k) => { __log.push("cache.put:" + k); },
    delete: async (k) => { __log.push("cache.delete:" + k); },
  }),
} });
globalThis.fetch = async (url) => { __log.push("fetch:" + url); throw new Error("offline (fake)"); };
const __tick = () => new Promise((r) => setTimeout(r, 20));
// A table body: records its HTML and click listeners.
globalThis.__tbody = (name) => {
  const t = __el(name);
  t.innerHTML = "";
  t.added = 0;
  t.addEventListener = (ev, fn) => { t.added++; t.listeners[ev] = fn; };
  t.querySelectorAll = () => [];
  return t;
};
// A click on a row (optionally on its cache button), as the browser
// delivers it to the table's listener.
globalThis.__click = (tbody, filepath, { onButton = false } = {}) => {
  const tr = filepath === null ? null : { dataset: { filepath } };
  const btn = onButton ? {
    disabled: false, textContent: "", innerHTML: "", title: "",
    classList: { toggle() {} },
    closest: (s) => (s === "tr[data-filepath]" ? tr : null),
  } : null;
  const target = { closest: (s) => (s === ".cache-btn" ? btn : s === "tr[data-filepath]" ? tr : null) };
  tbody.listeners.click({ target, currentTarget: tbody });
};
"""

SCORE_SIBLINGS = ["dom", "state", "viewer"]
LIBRARY = [
    {"filepath": "/m/Bach - Suite.pdf", "composer": "Bach", "title": "Suite",
     "tags": ["baroque", "cello"], "filename_tags": ["cello"]},
    {"filepath": '/m/O\'Neill - "Air" & <Song>.pdf', "composer": "O'Neill",
     "title": '"Air" & <Song>', "tags": []},
    {"filepath": "/m/Dvořák - Humoresque.pdf", "composer": "Dvořák",
     "title": "Humoresque", "tags": ["romantic"]},
]


class _Rows(HTMLParser):
    """Rows of a rendered tbody: attributes, cells and the cache button."""

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self._in = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self.rows.append({"attrs": a, "cells": [], "button": None})
        elif tag == "td":
            self.rows[-1]["cells"].append({"attrs": a, "text": ""})
            self._in = self.rows[-1]["cells"][-1]
        elif tag == "button":
            self.rows[-1]["button"] = {"class": a.get("class", "").split(), "title": a.get("title")}
            self._in = None

    def handle_endtag(self, tag):
        if tag == "td":
            self._in = None

    def handle_data(self, data):
        if self._in is not None:
            self._in["text"] += data


def _rows(html: str) -> list[dict]:
    p = _Rows()
    p.feed(html)
    return p.rows


def _render(body: str, entries: list | None = None) -> dict:
    return run_module(MODULES / "score-table.js", SCORE_SIBLINGS, f"""
for (const e of {json.dumps(entries or [])}) __lru.set(e.path, e);
const cache = await import("{(MODULES / 'cache.js').as_uri()}");
await cache.refreshCacheStatus(__tbody("scratch"));
const lib = __tbody("libraryBody");
{body}
await __tick();
console.log(JSON.stringify({{ html: lib.innerHTML, log: __log, added: lib.added }}));
""", setup=FAKE_STORAGE)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_rows_show_each_score_exactly():
    """Values with quotes, &, < and non-ASCII come back from the markup as
    they went in: the row's path, each cell and each cell's tooltip."""
    rows = _rows(_render(f"M.renderScoreRows(lib, {json.dumps(LIBRARY)});")["html"])
    assert [r["attrs"] for r in rows] == [{"data-filepath": s["filepath"]} for s in LIBRARY]
    for row, sc in zip(rows, LIBRARY):
        tags = ", ".join(sc["tags"])
        assert row["cells"][:3] == [
            {"attrs": {"title": sc["composer"]}, "text": sc["composer"]},
            {"attrs": {"title": sc["title"]}, "text": sc["title"]},
            {"attrs": {"title": tags}, "text": tags},
        ]
        assert row["cells"][3]["attrs"] == {"class": "cache-col"}


def test_an_extra_column_sits_between_tags_and_the_cache_button():
    rows = _rows(_render(
        f'M.renderScoreRows(lib, {json.dumps(LIBRARY[:1])}, {{ extra: (it) => "when:" + it.title }});',
    )["html"])
    assert [c["text"] for c in rows[0]["cells"]] == ["Bach", "Suite", "baroque, cello", "when:Suite", ""]


def test_missing_tags_render_as_an_empty_cell():
    rows = _rows(_render('M.renderScoreRows(lib, [{ filepath: "/x.pdf", composer: "A", title: "B" }]);')["html"])
    assert rows[0]["cells"][2] == {"attrs": {"title": ""}, "text": ""}


def test_an_empty_list_clears_the_table():
    r = _render(f'M.renderScoreRows(lib, {json.dumps(LIBRARY)}); M.renderScoreRows(lib, []);')
    assert r["html"] == ""


# The three states a cache button can start in, and what refreshCacheStatus
# (applyCacheButtonState) gives each.
CACHE_STATES = [
    ({"path": "/m/Bach - Suite.pdf", "pinned": True}, ["cache-btn", "small-btn", "cached"],
     "Pinned for offline use (click to remove)"),
    ({"path": "/m/Bach - Suite.pdf", "pinned": False}, ["cache-btn", "small-btn", "auto-cached"],
     "Auto-cached (click to pin)"),
    (None, ["cache-btn", "small-btn"], "Download for offline use"),
]


@pytest.mark.parametrize("entry, classes, title", CACHE_STATES, ids=["pinned", "auto", "none"])
def test_cache_button_renders_in_its_refreshed_state(entry, classes, title):
    """Regression: rows first rendered an auto-cached score as pinned
    ("Remove from offline cache") until refreshCacheStatus corrected it."""
    r = _render(f"M.renderScoreRows(lib, {json.dumps(LIBRARY[:1])});",
                [entry] if entry else [])
    button = _rows(r["html"])[0]["button"]
    assert button == {"class": classes, "title": title}


@pytest.mark.parametrize("entry", [s[0] for s in CACHE_STATES], ids=["pinned", "auto", "none"])
def test_rendered_button_matches_what_a_refresh_applies(entry):
    """The initial markup and refreshCacheStatus agree on every state."""
    r = run_module(MODULES / "cache.js", ["dom"], f"""
for (const e of {json.dumps([entry] if entry else [])}) __lru.set(e.path, e);
const classes = new Set(["cache-btn", "small-btn"]);
const btn = {{ innerHTML: "", title: "", classList: {{ toggle: (c, on) => on ? classes.add(c) : classes.delete(c) }} }};
const tbody = {{ querySelectorAll: () => [{{ dataset: {{ filepath: "/m/Bach - Suite.pdf" }}, querySelector: () => btn }}] }};
await M.refreshCacheStatus(tbody);
console.log(JSON.stringify({{ html: M.cacheButtonHtml("/m/Bach - Suite.pdf"),
  applied: {{ class: [...classes].sort(), title: btn.title, icon: btn.innerHTML }} }}));
""", setup=FAKE_STORAGE)
    [row] = _rows("<tr><td>" + r["html"] + "</td></tr>")
    assert sorted(row["button"]["class"]) == r["applied"]["class"]
    assert row["button"]["title"] == r["applied"]["title"]
    assert r["applied"]["icon"] in r["html"]


# ---------------------------------------------------------------------------
# Clicks (one delegated listener per table)
# ---------------------------------------------------------------------------


def test_clicking_a_row_opens_that_score():
    r = _render(f"""
M.renderScoreRows(lib, {json.dumps(LIBRARY)});
__click(lib, {json.dumps(LIBRARY[1]["filepath"])});
""")
    assert r["log"] == ["viewer:openScore:" + json.dumps([LIBRARY[1]], separators=(",", ":"))]


def test_clicking_a_cache_button_toggles_it_and_does_not_open():
    path = LIBRARY[1]["filepath"]
    r = _render(f"""
M.renderScoreRows(lib, {json.dumps(LIBRARY)});
__click(lib, {json.dumps(path)}, {{ onButton: true }});
""")
    assert not any(e.startswith("viewer:") for e in r["log"])
    assert r["log"][0] == "cache.match:/api/pdf?path=" + _encode(path)


def test_clicking_a_pinned_button_removes_the_offline_copy():
    path = LIBRARY[0]["filepath"]
    r = _render(f"""
M.renderScoreRows(lib, {json.dumps(LIBRARY)});
__click(lib, {json.dumps(path)}, {{ onButton: true }});
""", [{"path": path, "pinned": True}])
    assert r["log"] == ["cache.delete:/api/pdf?path=" + _encode(path)]


def test_a_click_outside_any_row_does_nothing():
    r = _render(f"M.renderScoreRows(lib, {json.dumps(LIBRARY)}); __click(lib, null);")
    assert r["log"] == []


def test_re_rendering_does_not_add_listeners():
    r = _render(f"for (let i = 0; i < 3; i++) M.renderScoreRows(lib, {json.dumps(LIBRARY)});")
    assert r["added"] == 1


def test_a_click_opens_the_current_render_of_that_table():
    """Each table keeps its own rows, and a re-render replaces them."""
    r = _render(f"""
const other = __tbody("recentBody");
M.renderScoreRows(lib, {json.dumps(LIBRARY)});
M.renderScoreRows(other, [{{ filepath: "/m/Bach - Suite.pdf", composer: "Bach", title: "Old" }}],
  {{ toOpen: (it) => ({{ which: "recent", title: it.title }}) }});
M.renderScoreRows(lib, [{{ filepath: "/m/Bach - Suite.pdf", composer: "Bach", title: "New" }}]);
__click(lib, "/m/Bach - Suite.pdf");
__click(other, "/m/Bach - Suite.pdf");
""")
    assert r["log"] == [
        'viewer:openScore:[{"filepath":"/m/Bach - Suite.pdf","composer":"Bach","title":"New"}]',
        'viewer:openScore:[{"which":"recent","title":"Old"}]',
    ]


def _encode(path: str) -> str:
    from urllib.parse import quote
    return quote(path, safe="-_.!~*'()")      # encodeURIComponent


# ---------------------------------------------------------------------------
# Recent and Newest
# ---------------------------------------------------------------------------

RECENT = [{"filepath": "/m/Bach - Suite.pdf", "composer": "Bach", "title": "Suite",
           "tags": ["stale"], "timestamp": 0}]


def _view(module: str, body: str, api_result: dict) -> dict:
    return run_module(MODULES / f"{module}.js", ["dom", "state", "api", "viewer"], f"""
__returns.api = async () => ({json.dumps(api_result)});
const body = __tbody("{module}Body");
{body}
await __tick();
console.log(JSON.stringify({{ html: body.innerHTML, status: __el("{module}Status").textContent, log: __log }}));
""", setup=FAKE_STORAGE)


def test_recent_opens_a_partial_record_without_its_stored_tags():
    r = _view("recent", f"""
await M.renderRecent();
__click(body, "/m/Bach - Suite.pdf");
""", {"recent": RECENT})
    rows = _rows(r["html"])
    assert [c["text"] for c in rows[0]["cells"][:3]] == ["Bach", "Suite", "stale"]
    assert rows[0]["cells"][3]["text"] != ""              # the "when" column
    assert r["status"] == "1 recent scores"
    assert r["log"][-1] == 'viewer:openScore:[{"filepath":"/m/Bach - Suite.pdf","composer":"Bach","title":"Suite"}]'


def test_newest_opens_the_full_score():
    sc = {**LIBRARY[0], "mtime": 0}
    r = _view("newest", "await M.renderNewest(); __click(body, '/m/Bach - Suite.pdf');",
              {"scores": [sc]})
    assert [c["text"] for c in _rows(r["html"])[0]["cells"][:4]] == ["Bach", "Suite", "baroque, cello", "Unknown"]
    assert r["log"][-1] == "viewer:openScore:" + json.dumps([sc], separators=(",", ":"), ensure_ascii=False)


@pytest.mark.parametrize("module, result, message", [
    ("recent", {"recent": []}, "No recently viewed scores."),
    ("newest", {"scores": []}, "No scores in library."),
])
def test_an_empty_list_clears_old_rows_and_says_so(module, result, message):
    r = _view(module, f"""
body.innerHTML = "<tr><td>old row</td></tr>";
await M.render{module.capitalize()}();
""", result)
    assert r["html"] == "" and r["status"] == message


# ---------------------------------------------------------------------------
# Setlist songs: the same cache-button handler
# ---------------------------------------------------------------------------


def test_on_cache_button_click_ignores_rows_without_a_path():
    """Setlist-reference rows have no data-filepath and no cache button."""
    r = run_module(MODULES / "cache.js", ["dom"], """
const onRef = { target: { closest: () => null } };
const btn = { closest: () => null };
const onStray = { target: { closest: (s) => (s === ".cache-btn" ? btn : null) } };
console.log(JSON.stringify([M.onCacheButtonClick(onRef), M.onCacheButtonClick(onStray), __log]));
""", setup=FAKE_STORAGE)
    assert r == [False, False, []]


def test_setlist_songs_table_uses_the_cache_button_handler():
    r = run_module(MODULES / "setlists.js",
                   ["dom", "state", "api", "views", "viewer", "cache", "library-filter"], """
M.initSetlistEvents();
__el("setlistSongsBody").listeners.click({ marker: 1 });
console.log(JSON.stringify(__log.filter((e) => e.startsWith("cache:onCacheButtonClick"))));
""", state={"editingSetlistItems": []}, setup=FAKE_STORAGE)
    assert r == ['cache:onCacheButtonClick:[{"marker":1}]']
