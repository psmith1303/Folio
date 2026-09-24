""""Download for offline" downloads once (Step 10c).

Before this change, cache.js's cachePdf() always fetched and stored the PDF
itself even though the service worker in front of it had already fetched
and stored the very same response on its way through (handlePdfFetch stores
before answering on a cache miss). cachePdf now pins what the service
worker already stored (pinIfCached) and only falls back to storing it
itself (storePdf) when there was no service worker, or its own storage
attempt failed (a truncated download).

Runs the real cache.js under Deno with offline-lru.js's real storePdf/
pinIfCached (js_module_harness stubs only dom.js), using the FAKES (in-memory
IndexedDB + Cache Storage) from test_offline_lru.py.
"""

import json

from deno_harness import requires_deno
from js_module_harness import MODULES
from test_offline_lru import FAKES, _key, _lru

pytestmark = requires_deno


def _page(body: str):
    """Run *body* against the real cache.js (js_module_harness.run_module),
    with only dom.js stubbed -- offline-lru.js is imported for real, as
    cache.js does -- using the FAKES in-memory IndexedDB/Cache Storage."""
    from js_module_harness import run_module
    return run_module(MODULES / "cache.js", ["dom"], body + "\nawait __tick();",
                      setup="globalThis.window = globalThis; globalThis.isSecureContext = true;\n" + FAKES)


PATH = "/m/Bach - Suite.pdf"


# ---------------------------------------------------------------------------
# Behind a real service worker: the SW has already stored what it fetched
# before answering, so cachePdf must not download or store it again.
# ---------------------------------------------------------------------------

def test_cache_pdf_behind_a_real_service_worker_downloads_once_and_pins():
    r = _page(f"""
// Count every cache.put, from wherever it's called -- the simulated service
// worker below, or (if cachePdf wrongly stores again) storePdf itself.
let putCalls = 0;
const realOpen = caches.open;
Object.defineProperty(globalThis, "caches", {{ configurable: true, value: {{
  open: async (name) => {{
    const c = await realOpen(name);
    return {{ ...c, put: async (...args) => {{ putCalls++; return c.put(...args); }} }};
  }},
}} }});
globalThis.fetch = async (req) => {{
  const url = typeof req === "string" ? req : req.url;
  __fetched.push(url);
  const body = "%PDF-1.4 " + {json.dumps(PATH)};
  const resp = new Response(body, {{ status: 200, headers: {{ "content-length": String(body.length) }} }});
  // The service worker stores what it fetched, before answering the page.
  const cache = await caches.open(M.PDF_CACHE);
  await cache.put(M.pdfCacheKey({json.dumps(PATH)}), resp.clone());
  return resp;
}};
await M.cachePdf({json.dumps(PATH)});
console.log(JSON.stringify({{ ...__state(), fetched: __fetched, putCalls }}));
""")
    assert len(r["fetched"]) == 1                       # exactly one network fetch
    assert r["fetched"][0].startswith(_key(PATH) + "&_t=")
    assert r["putCalls"] == 1                            # the SW stored it; the page didn't store again
    assert r["stores"]["folio-pdfs-v2"] == [_key(PATH)]
    assert r["lru"][PATH]["pinned"] is True


def test_cache_pdf_without_a_service_worker_stores_it_itself():
    """No SW in front (or it didn't intercept): pinIfCached finds nothing,
    so cachePdf must store the PDF itself."""
    r = _page(f"""
await M.cachePdf({json.dumps(PATH)});
console.log(JSON.stringify({{ ...__state(), fetched: __fetched }}));
""")
    assert len(r["fetched"]) == 1
    assert r["stores"]["folio-pdfs-v2"] == [_key(PATH)]
    assert r["lru"][PATH]["pinned"] is True


def test_cache_pdf_truncated_with_no_service_worker_throws_and_stores_nothing():
    r = _page(f"""
globalThis.fetch = async () => new Response("short",
  {{ status: 200, headers: {{ "content-length": "999" }} }});
let error = null;
try {{ await M.cachePdf({json.dumps(PATH)}); }} catch (e) {{ error = e.message; }}
console.log(JSON.stringify({{ error, ...__state() }}));
""")
    assert r["error"] == "Incomplete download — not caching"
    assert r["stores"].get("folio-pdfs-v2", []) == []
    assert PATH not in r["lru"]


def test_pin_pdf_on_an_already_cached_pdf_makes_no_network_fetch():
    """pinPdf (e.g. "Cache setlist" on an already auto-cached PDF) must not
    re-download; a bug here would double every setlist download."""
    r = _page(f"""
await self.FolioLru.touchLruEntry({json.dumps(PATH)}, 42, false);
(await caches.open(M.PDF_CACHE)).put(M.pdfCacheKey({json.dumps(PATH)}), new Response("x"));
await M.pinPdf({json.dumps(PATH)});
console.log(JSON.stringify({{ ...__state(), fetched: __fetched }}));
""")
    assert r["fetched"] == []
    assert r["lru"][PATH] == {"size": 42, "pinned": True, "lastUsed": 1000}


# ---------------------------------------------------------------------------
# storePdf / pinIfCached directly (offline-lru.js) -- these are new exports
# with no HEAD equivalent to certify against.
# ---------------------------------------------------------------------------


def test_store_pdf_records_the_size_from_actual_bytes_not_the_header():
    """A multi-byte UTF-8 body: byteLength differs from the JS string's
    .length, so a bug that recorded the wrong count would show up here."""
    body = "café ☕ score"
    r = _lru(f"""
const bytes = new TextEncoder().encode({json.dumps(body)});
const resp = new Response(bytes, {{ status: 200,
  headers: {{ "content-length": String(bytes.byteLength) }} }});
const ok = await L.storePdf("/a.pdf", resp, false);
console.log(JSON.stringify({{ ok, size: __state().lru["/a.pdf"].size,
  byteLength: bytes.byteLength, strLength: {json.dumps(body)}.length }}));
""")
    assert r["ok"] is True
    assert r["strLength"] != r["byteLength"]            # multi-byte chars: a real test of the two
    assert r["size"] == r["byteLength"]                  # bytes recorded, not the JS string length


def test_store_pdf_truncated_body_returns_false_and_stores_nothing():
    r = _lru("""
const resp = new Response("short", { status: 200, headers: { "content-length": "999" } });
const ok = await L.storePdf("/a.pdf", resp, false);
console.log(JSON.stringify({ ok, ...__state() }));
""")
    assert r["ok"] is False
    assert r["stores"].get(r"folio-pdfs-v2", []) == []
    assert "/a.pdf" not in r["lru"]


def test_store_pdf_eviction_failure_does_not_fail_the_store():
    """evictIfNeeded runs un-awaited; a failure there (e.g. Cache Storage
    quota) must not stop storePdf from having already stored the PDF and
    recorded it."""
    r = _lru("""
// Fill past MAX_AUTO_CACHED so storePdf's un-awaited evictIfNeeded() will
// actually try to evict (and so hit our broken cache.delete).
for (let i = 0; i < 100; i++) {
  __setNow(10 + i);
  await L.touchLruEntry(`/u${i}.pdf`, 1, false);
  (await caches.open(L.PDF_CACHE)).put(L.pdfCacheKey(`/u${i}.pdf`), new Response("x"));
}
const realOpen = caches.open;
Object.defineProperty(globalThis, "caches", { configurable: true, value: {
  open: async (name) => {
    const c = await realOpen(name);
    return { ...c, delete: async () => { throw new Error("simulated eviction failure"); } };
  },
} });
__setNow(9999);
const resp = new Response("%PDF-1.4 new", { status: 200,
  headers: { "content-length": String("%PDF-1.4 new".length) } });
const ok = await L.storePdf("/new.pdf", resp, false);
await __tick(50);   // let the un-awaited evictIfNeeded()/catch settle
console.log(JSON.stringify({ ok, stored: (await realOpen(L.PDF_CACHE)).match !== undefined,
  cached: !!(await (await caches.open(L.PDF_CACHE)).match(L.pdfCacheKey("/new.pdf"))),
  lru: !!__state().lru["/new.pdf"] }));
""")
    assert r["ok"] is True
    assert r["cached"] is True
    assert r["lru"] is True


def test_pin_if_cached_on_an_uncached_path_returns_false_and_changes_nothing():
    r = _lru("""
const ok = await L.pinIfCached("/nope.pdf");
console.log(JSON.stringify({ ok, ...__state() }));
""")
    assert r["ok"] is False
    assert r["lru"] == {}
    assert r["stores"].get(r"folio-pdfs-v2", []) == []
