"""Conditional revalidation of GET /api/pdf, server and service worker
(Step 10c).

/api/pdf now answers 304 (ETag echoed, no body) when If-None-Match names the
current ETag, so the service worker can check a cached PDF is still current
without re-downloading it. The ETag is Starlette's FileResponse default:
md5 of f"{st_mtime}-{st_size}", so it changes whenever the file is replaced.
A 304 must never happen for a path that traversal or 404 would otherwise
reject -- the freshness check only runs after those checks pass.

The second half runs the REAL sw.js under Deno (boot machinery from
test_offline_lru.py): on a cache hit it now serves the cached copy and
revalidates it in the background with If-None-Match, instead of always
re-downloading the whole file.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

import web.server as srv
from web.server import app, state

from deno_harness import requires_deno
from test_offline_lru import FAKES, _sw

PATH = "Bach - Suite.pdf"


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Isolate server config and state, as in test_web_api.py."""
    monkeypatch.setattr(srv, "WEB_CONFIG_PATH", str(tmp_path / "web_config.json"))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(srv, "CONFIG_DIR", str(config_dir))
    state.library_dir = ""
    state.scores = []
    state.config = {"last_directory": "", "allowed_roots": []}
    srv._rate_buckets.clear()
    yield
    state.library_dir = ""
    state.scores = []
    state.config = {"last_directory": "", "allowed_roots": []}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def lib(tmp_path, client):
    (tmp_path / PATH).write_bytes(b"%PDF-1.4 bach")
    assert client.post("/api/library", json={"path": str(tmp_path)}).status_code == 200
    return tmp_path


def _etag(client):
    return client.get(f"/api/pdf?path={PATH}").headers["etag"]


# ---------------------------------------------------------------------------
# Matching If-None-Match -> 304
# ---------------------------------------------------------------------------


def test_matching_etag_returns_304_with_no_body(client, lib):
    etag = _etag(client)
    resp = client.get(f"/api/pdf?path={PATH}", headers={"If-None-Match": etag})
    assert resp.status_code == 304
    assert resp.content == b""
    assert resp.headers["etag"] == etag
    assert resp.headers["cache-control"] == "no-cache"


def test_matching_weak_etag_from_the_client_matches(client, lib):
    """A cache (or this client) may hold the tag as weak; the server's
    strong tag must still match it."""
    etag = _etag(client)
    resp = client.get(f"/api/pdf?path={PATH}", headers={"If-None-Match": f'W/{etag}'})
    assert resp.status_code == 304


def test_a_weak_server_etag_matches_a_bare_client_tag(monkeypatch, client, lib):
    """_etag_matches strips W/ on both sides; simulate a weak server tag by
    monkeypatching it directly, since FileResponse's own tag is strong."""
    assert srv._etag_matches("abc", 'W/abc')
    assert srv._etag_matches('W/abc', "abc")
    assert srv._etag_matches('W/"abc"', '"abc"')


def test_matching_tag_inside_a_comma_separated_list(client, lib):
    etag = _etag(client)
    resp = client.get(f"/api/pdf?path={PATH}",
                       headers={"If-None-Match": f'"bogus", {etag}, W/"other"'})
    assert resp.status_code == 304


# ---------------------------------------------------------------------------
# Non-matching / absent If-None-Match -> 200 with full body
# ---------------------------------------------------------------------------


def test_no_if_none_match_header_returns_200(client, lib):
    resp = client.get(f"/api/pdf?path={PATH}")
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4 bach"
    assert "etag" in resp.headers


def test_non_matching_etag_returns_200_with_full_body(client, lib):
    resp = client.get(f"/api/pdf?path={PATH}", headers={"If-None-Match": '"not-the-tag"'})
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4 bach"


def test_file_changed_since_invalidates_the_old_etag(client, lib):
    """Even though the client sends back exactly the tag it was given, a
    file change (mtime/size) after that must force a fresh 200, not a 304
    for stale content."""
    old_etag = _etag(client)
    os.utime(lib / PATH, (2_000_000_000, 2_000_000_000))
    resp = client.get(f"/api/pdf?path={PATH}", headers={"If-None-Match": old_etag})
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4 bach"
    assert resp.headers["etag"] != old_etag


# ---------------------------------------------------------------------------
# Traversal / 404 unaffected -- a 304 must never leak past those checks
# ---------------------------------------------------------------------------


def test_traversal_outside_library_is_still_403_regardless_of_if_none_match(client, lib):
    resp = client.get("/api/pdf?path=../secret.pdf", headers={"If-None-Match": "*"})
    assert resp.status_code == 403


def test_missing_file_is_still_404_regardless_of_if_none_match(client, lib):
    resp = client.get("/api/pdf?path=nope.pdf", headers={"If-None-Match": "*"})
    assert resp.status_code == 404


def test_no_library_set_is_still_400_regardless_of_if_none_match(client):
    resp = client.get("/api/pdf?path=x.pdf", headers={"If-None-Match": "*"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# The service worker: cache hit revalidates in the background instead of
# always re-downloading; cache miss stores before answering.
# ---------------------------------------------------------------------------

pytestmark = requires_deno

# A fetch stub that speaks ETags: records every request (url, If-None-Match,
# cache mode) and answers 304 when the request's If-None-Match names the
# current version's etag, else 200 with that version's body and etag.
# __setVersion lets a test change "the PDF on the server" mid-flight.
CONDITIONAL_FETCH = r"""
globalThis.__reqs = [];
globalThis.__version = { etag: '"v1"', body: "%PDF-1.4 v1" };
globalThis.__setVersion = (etag, body) => { __version = { etag, body }; };
globalThis.fetch = async (req, opts) => {
  const url = typeof req === "string" ? req : req.url;
  let inm = null;
  if (opts && opts.headers && Object.prototype.hasOwnProperty.call(opts.headers, "If-None-Match")) {
    inm = opts.headers["If-None-Match"];
  } else if (req && req.headers && typeof req.headers.get === "function") {
    inm = req.headers.get("if-none-match");
  }
  const cacheMode = opts && "cache" in opts ? opts.cache : null;
  __reqs.push({ url, inm, cache: cacheMode });
  const v = __version;
  if (inm && inm === v.etag) {
    return new Response(null, { status: 304, headers: { etag: v.etag } });
  }
  return new Response(v.body, { status: 200, headers: { "content-length": String(v.body.length), etag: v.etag } });
};
"""


def test_cache_hit_serves_the_cached_copy_immediately_even_if_the_server_changed():
    """Stale-while-revalidate: the answer is the OLD cached bytes, served
    before the background fetch can have any effect -- not a blocking
    revalidate-then-serve."""
    path = "/m/Bach - Suite.pdf"
    r = _sw(CONDITIONAL_FETCH + f"""
await fetchPdf({json.dumps(path)});                     // stores v1
__setVersion('"v2"', "%PDF-1.4 v2 CHANGED");             // the server now has a newer PDF
let resp;
listeners.fetch({{
  request: new Request("/api/pdf?path=" + encodeURIComponent({json.dumps(path)})),
  respondWith: (p) => {{ resp = p; }}, waitUntil: () => {{}},
}});
const served = await resp;
const body = await served.text();
console.log(JSON.stringify({{ status: served.status, body }}));
""")
    assert r["status"] == 200
    assert r["body"] == "%PDF-1.4 v1"


def test_cache_hit_background_request_carries_if_none_match_and_no_store():
    path = "/m/Bach - Suite.pdf"
    r = _sw(CONDITIONAL_FETCH + f"""
await fetchPdf({json.dumps(path)});           // cache miss: stores v1 (reqs[0])
__setNow(5000);
await fetchPdf({json.dumps(path)});           // cache hit: revalidates in the background (reqs[1])
console.log(JSON.stringify({{ reqs: __reqs }}));
""")
    assert len(r["reqs"]) == 2
    assert r["reqs"][0]["inm"] is None                 # the initial download is unconditional
    assert r["reqs"][1]["inm"] == '"v1"'                # revalidation names the cached copy's etag
    assert r["reqs"][1]["cache"] == "no-store"


def test_304_on_revalidation_leaves_the_cached_bytes_unchanged_and_is_not_restored():
    path = "/m/Bach - Suite.pdf"
    r = _sw(CONDITIONAL_FETCH + f"""
await fetchPdf({json.dumps(path)});           // stores v1
__setNow(5000);
await fetchPdf({json.dumps(path)});           // background revalidation gets 304 (version unchanged)
const cached = await (await caches.open(self.FolioLru.PDF_CACHE)).match(
  self.FolioLru.pdfCacheKey({json.dumps(path)}));
console.log(JSON.stringify({{ reqs: __reqs, body: await cached.text(),
  lastUsed: __state().lru[{json.dumps(path)}].lastUsed }}));
""")
    assert len(r["reqs"]) == 2
    assert r["body"] == "%PDF-1.4 v1"                   # unchanged; not re-stored
    assert r["lastUsed"] == 5000                         # the hit itself still recorded the use


def test_200_on_revalidation_replaces_the_cached_bytes_and_stays_unpinned():
    path = "/m/Bach - Suite.pdf"
    r = _sw(CONDITIONAL_FETCH + f"""
await fetchPdf({json.dumps(path)});                     // stores v1
__setVersion('"v2"', "%PDF-1.4 v2 CHANGED");             // the PDF changed on the server
__setNow(5000);
await fetchPdf({json.dumps(path)});                     // background revalidation gets 200
const cached = await (await caches.open(self.FolioLru.PDF_CACHE)).match(
  self.FolioLru.pdfCacheKey({json.dumps(path)}));
console.log(JSON.stringify({{ body: await cached.text(),
  pinned: __state().lru[{json.dumps(path)}].pinned }}));
""")
    assert r["body"] == "%PDF-1.4 v2 CHANGED"
    assert r["pinned"] is False


def test_200_on_revalidation_keeps_a_pinned_pdf_pinned():
    path = "/m/Bach - Suite.pdf"
    r = _sw(CONDITIONAL_FETCH + f"""
await fetchPdf({json.dumps(path)});
await self.FolioLru.touchLruEntry({json.dumps(path)}, 9, true);   // the user pinned it
__setVersion('"v2"', "%PDF-1.4 v2 CHANGED");
await fetchPdf({json.dumps(path)});                     // background revalidation gets 200
console.log(JSON.stringify({{ pinned: __state().lru[{json.dumps(path)}].pinned,
  body: await (await (await caches.open(self.FolioLru.PDF_CACHE)).match(
    self.FolioLru.pdfCacheKey({json.dumps(path)}))).text() }}));
""")
    assert r["pinned"] is True
    assert r["body"] == "%PDF-1.4 v2 CHANGED"


def test_a_cached_copy_with_no_etag_still_revalidates_unconditionally():
    """No stored etag (e.g. an older cache entry) means no If-None-Match can
    be sent, but the background fetch must still happen -- it must not be
    skipped just because there's nothing to compare."""
    path = "/m/Bach - Suite.pdf"
    r = _sw(CONDITIONAL_FETCH + f"""
const cache = await caches.open(self.FolioLru.PDF_CACHE);
await cache.put(self.FolioLru.pdfCacheKey({json.dumps(path)}), new Response("%PDF-1.4 no-etag"));
await self.FolioLru.touchLruEntry({json.dumps(path)}, 10, false);
await fetchPdf({json.dumps(path)});
console.log(JSON.stringify({{ reqs: __reqs }}));
""")
    assert len(r["reqs"]) == 1
    assert r["reqs"][0]["inm"] is None


def test_cache_miss_stores_before_the_response_is_returned():
    """A page's cachePdf awaits this same fetch; if storage happened after
    the response were returned, a page racing to pin it right after could
    find nothing cached yet."""
    path = "/m/Bach - Suite.pdf"
    r = _sw(CONDITIONAL_FETCH + f"""
let resp;
listeners.fetch({{
  request: new Request("/api/pdf?path=" + encodeURIComponent({json.dumps(path)})),
  respondWith: (p) => {{ resp = p; }}, waitUntil: () => {{}},
}});
const r = await resp;                       // no extra settling time added
const cached = await (await caches.open(self.FolioLru.PDF_CACHE)).match(
  self.FolioLru.pdfCacheKey({json.dumps(path)}));
console.log(JSON.stringify({{ status: r.status, storedByThen: !!cached }}));
""")
    assert r["status"] == 200
    assert r["storedByThen"] is True


def test_cache_miss_truncated_response_is_not_stored():
    path = "/m/Bach - Suite.pdf"
    r = _sw(f"""
globalThis.fetch = async () => new Response("short",
  {{ status: 200, headers: {{ "content-length": "999" }} }});
const status = await fetchPdf({json.dumps(path)});
console.log(JSON.stringify({{ status, ...__state() }}));
""")
    assert r["status"] == 200                # the (truncated) response still reaches the caller
    assert r["stores"].get("folio-pdfs-v2", []) == []
    assert path not in r["lru"]


def test_no_library_set_is_still_400_regardless_of_if_none_match(client):
    resp = client.get("/api/pdf?path=x.pdf", headers={"If-None-Match": "*"})
    assert resp.status_code == 400
