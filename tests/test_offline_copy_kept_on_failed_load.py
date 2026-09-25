"""A failed PDF open must never delete the cached copy (v2.15.1).

Bug: on the iPad, some previously downloaded PDFs wouldn't open in Airplane
Mode although their Library row showed them cached. Cause: viewer.js's
_fetchPdfDoc, when a pdf.js load failed, purged the PDF's Cache Storage
entry (PDF_CACHE) before retrying -- offline, the retry then failed too (the
service worker answers 503), so the pinned copy was lost for good while the
LRU IndexedDB entry still said it was cached.

Fix: _fetchPdfDoc no longer deletes anything. On failure it fetches the PDF
marked `reload=1` -- the service worker either downloads and stores a fresh
copy (online) or answers 503 while leaving the cached copy in place
(offline) -- and only retries pdf.js if that fetch succeeded, loading the
retry from the downloaded bytes (so a copy the service worker couldn't store
doesn't matter); otherwise it rethrows the original pdf.js error, cached
copy untouched. The marker is in the URL because a browser needn't pass a
fetch's `cache` mode on to the service worker.

Runs the REAL shipped source under Deno: _fetchPdfDoc sliced out of
viewer.js (deno_harness.slice_source), and sw.js's handlePdfFetch via the
_sw/FAKES boot machinery from test_offline_lru.py (also used by
test_pdf_revalidation.py and test_pdf_cache_write_failure.py).
"""

import json
import re
from pathlib import Path

import pytest

from deno_harness import requires_deno, run_deno, slice_source
from test_offline_lru import _key, _sw

pytestmark = requires_deno

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
MODULES = STATIC / "modules"
VIEWER_JS = MODULES / "viewer.js"
SW_JS = STATIC / "sw.js"

# ---------------------------------------------------------------------------
# viewer.js _fetchPdfDoc, sliced out of the real file and run standalone.
#
# Stubs everything it references: pdfjsLib.getDocument (a scripted queue of
# results), pdfCacheKey, PDFJS_BASE, VIEWER_TAG, showToast, fetch (a
# scripted implementation) -- plus PDF_CACHE and caches.open/delete, which
# only the PRE-FIX function body uses, so the same harness runs unmodified
# against the pre-fix source during certification (see the certification
# script, not part of this file).
# ---------------------------------------------------------------------------

FAKES = r"""
globalThis.VIEWER_TAG = "[viewer]";
globalThis.PDFJS_BASE = "/lib/pdfjs";
globalThis.PDF_CACHE = "folio-pdfs-v2";
globalThis.pdfCacheKey = (path) => "/api/pdf?path=" + encodeURIComponent(path);

// A pre-seeded Cache Storage entry for the PDF under test, so a test can
// tell whether _fetchPdfDoc deleted it.
globalThis.__cacheStore = new Map([["/api/pdf?path=song.pdf", "cached-bytes"]]);
Object.defineProperty(globalThis, "caches", { configurable: true, value: {
  open: async () => ({
    delete: async (key) => __cacheStore.delete(key),
    match: async (key) => __cacheStore.get(key),
  }),
} });

globalThis.__toasts = [];
globalThis.showToast = (msg) => { __toasts.push(msg); };

// getDocument() results, consumed in order; each is {error} or {value}.
globalThis.__docQueue = [];
globalThis.__getDocCalls = [];
globalThis.pdfjsLib = {
  getDocument: (opts) => {
    __getDocCalls.push(opts);
    const next = __docQueue.shift() || { error: new Error("unconfigured") };
    return { promise: (async () => {
      if (next.error) throw next.error;
      return next.value;
    })() };
  },
};

// fetch() -- overridden per test as __fetchImpl.
globalThis.__fetchCalls = [];
globalThis.__fetchImpl = async () => { throw new Error("fetch not configured"); };
globalThis.fetch = (...args) => { __fetchCalls.push(args); return globalThis.__fetchImpl(...args); };
"""


def _fetch_pdf_doc(body: str):
    """Run *body* with the real, current _fetchPdfDoc loaded (sliced fresh
    from viewer.js on disk, so certification against a reverted file needs
    no changes to this helper)."""
    src = VIEWER_JS.read_text(encoding="utf-8")
    fn = slice_source(src, "async function _fetchPdfDoc", "async function _fetchAnnotations")
    return run_deno(FAKES + fn + "\n" + body)


# ---------------------------------------------------------------------------
# (a) Regression: offline retry must not lose the cached copy.
# ---------------------------------------------------------------------------

OFFLINE_RELOAD_FAILURES = {
    "reload fetch throws (network error)": 'globalThis.__fetchImpl = async () => { throw new TypeError("offline"); };',
    "reload fetch resolves 503 (SW offline path)":
        'globalThis.__fetchImpl = async () => new Response("Offline", { status: 503 });',
}


@pytest.mark.parametrize("setup", OFFLINE_RELOAD_FAILURES.values(), ids=OFFLINE_RELOAD_FAILURES.keys())
def test_offline_failed_reload_keeps_the_cached_copy_and_rethrows_the_original_error(setup):
    """REGRESSION test: fails on HEAD's viewer.js, which deletes the
    PDF_CACHE entry before retrying, losing it for good when the retry (as
    here) also fails offline."""
    r = _fetch_pdf_doc(setup + """
__docQueue.push({ error: new Error("bad end offset") });
let thrown = null;
try {
  await _fetchPdfDoc("song.pdf");
} catch (e) {
  thrown = e.message;
}
console.log(JSON.stringify({
  thrown, getDocCalls: __getDocCalls.length,
  cacheHasEntry: __cacheStore.has("/api/pdf?path=song.pdf"),
}));
""")
    assert r["thrown"] == "bad end offset"          # the ORIGINAL error, not a fetch error
    assert r["getDocCalls"] == 1                    # no second pdf.js attempt
    assert r["cacheHasEntry"] is True                # nothing deleted


# ---------------------------------------------------------------------------
# (b) Online: a successful reload retries pdf.js once more.
# ---------------------------------------------------------------------------

FRESH = "__fetchImpl = async () => ({ ok: true, arrayBuffer: async () => new Uint8Array([7, 8, 9]).buffer });"


def test_online_successful_reload_retries_from_the_downloaded_bytes():
    """The retry loads the bytes the reload fetched, not the cache: if the
    service worker couldn't store them (storage full, a truncated body), the
    cache still holds the bad copy that just failed."""
    r = _fetch_pdf_doc(FRESH + """
__docQueue.push({ error: new Error("corrupt") }, { value: "PDFDOC2" });
const result = await _fetchPdfDoc("song.pdf");
const [first, second] = __getDocCalls;
console.log(JSON.stringify({
  result, getDocCalls: __getDocCalls.length, fetchCalls: __fetchCalls,
  firstUrl: first.url, secondUrl: second.url ?? null,
  secondData: second.data ? Array.from(second.data) : null,
  secondWasm: second.wasmUrl, secondRange: second.disableRange,
}));
""")
    assert r["result"] == "PDFDOC2"
    assert r["getDocCalls"] == 2
    assert r["firstUrl"].startswith("/api/pdf?path=song.pdf&_t=")
    assert len(r["fetchCalls"]) == 1
    url, opts = r["fetchCalls"][0]
    assert url == "/api/pdf?path=song.pdf&reload=1"  # marked in the URL for the service worker
    assert opts == {"cache": "reload"}
    assert r["secondUrl"] is None                     # not re-read from the cache...
    assert r["secondData"] == [7, 8, 9]               # ...but from the fresh download
    assert r["secondWasm"] == "/lib/pdfjs/wasm/" and r["secondRange"] is True


# ---------------------------------------------------------------------------
# (c) Online, but the retried pdf.js load also fails: that error propagates.
# ---------------------------------------------------------------------------

def test_second_load_failure_propagates():
    r = _fetch_pdf_doc(FRESH + """
__docQueue.push({ error: new Error("corrupt") }, { error: new Error("still bad") });
let thrown = null;
try {
  await _fetchPdfDoc("song.pdf");
} catch (e) {
  thrown = e.message;
}
console.log(JSON.stringify({ thrown, getDocCalls: __getDocCalls.length }));
""")
    assert r["thrown"] == "still bad"
    assert r["getDocCalls"] == 2


def test_showRetryToast_false_suppresses_the_toast_but_still_retries():
    r = _fetch_pdf_doc(FRESH + """
__docQueue.push({ error: new Error("corrupt") }, { value: "PDFDOC2" });
const result = await _fetchPdfDoc("song.pdf", { showRetryToast: false });
console.log(JSON.stringify({ result, toasts: __toasts }));
""")
    assert r["result"] == "PDFDOC2"
    assert r["toasts"] == []


# ---------------------------------------------------------------------------
# sw.js handlePdfFetch: a `reload=1` request skips the cache. The requests
# carry no `cache` mode, so these pass only if the URL marker is honoured.
# ---------------------------------------------------------------------------

def test_reload_request_downloads_even_with_a_cached_copy_and_keeps_a_pin():
    """(d) Online: a reload request is answered with a FRESH download, not
    the stale cached bytes, even though a cached copy exists -- proving the
    cache is actually skipped, not just revalidated in the background (which
    happens on every ordinary hit too and would otherwise make this pass by
    coincidence). The fresh body is also stored, and a pinned entry stays
    pinned."""
    path = "/m/Bach - Suite.pdf"
    r = _sw(f"""
await fetchPdf({json.dumps(path)});                              // stores the OLD cached bytes
await self.FolioLru.touchLruEntry({json.dumps(path)}, 9, true);  // the user pinned it
globalThis.fetch = async () => new Response("CHANGED", {{ status: 200, headers: {{ "content-length": "7" }} }});
let resp;
listeners.fetch({{
  request: new Request("/api/pdf?path=" + encodeURIComponent({json.dumps(path)}) + "&reload=1"),
  respondWith: (p) => {{ resp = p; }}, waitUntil: () => {{}},
}});
const r = await resp;
const body = await r.text();
const cached = await (await caches.open(self.FolioLru.PDF_CACHE)).match(
  self.FolioLru.pdfCacheKey({json.dumps(path)}));
console.log(JSON.stringify({{ status: r.status, body,
  cachedBody: await cached.text(), pinned: __state().lru[{json.dumps(path)}].pinned }}));
""")
    assert r["status"] == 200
    assert r["body"] == "CHANGED"                     # the FRESH download, not the stale cached copy
    assert r["cachedBody"] == "CHANGED"               # ...and it replaced the cached bytes
    assert r["pinned"] is True


def test_reload_request_offline_answers_503_and_leaves_the_cached_copy_unchanged():
    """(e) Offline: a reload request gets the same 503 a miss would, and
    the existing cached copy is untouched (still there, still the old
    bytes) -- it is never cleared just because a fresh copy was wanted."""
    path = "/m/Bach - Suite.pdf"
    r = _sw(f"""
await fetchPdf({json.dumps(path)});                     // stores v1 while still "online"
globalThis.fetch = async () => {{ throw new TypeError("offline"); }};
let resp;
listeners.fetch({{
  request: new Request("/api/pdf?path=" + encodeURIComponent({json.dumps(path)}) + "&reload=1"),
  respondWith: (p) => {{ resp = p; }}, waitUntil: () => {{}},
}});
const r = await resp;
const cached = await (await caches.open(self.FolioLru.PDF_CACHE)).match(
  self.FolioLru.pdfCacheKey({json.dumps(path)}));
console.log(JSON.stringify({{ status: r.status, body: await cached.text() }}));
""")
    assert r["status"] == 503
    assert r["body"] == "%PDF-1.4 https://folio.test" + _key(path)


def test_normal_request_with_a_cached_copy_still_serves_the_cache_unchanged():
    """(f) A plain (non-reload) request must keep behaving as before: it is
    answered from the cache immediately, even though the server now has
    different bytes (background revalidation happens, but doesn't block or
    change this response) -- proving the new `reload=1` check in
    handlePdfFetch didn't disturb the ordinary hit path."""
    path = "/m/Bach - Suite.pdf"
    r = _sw(f"""
await fetchPdf({json.dumps(path)});           // stores v1
globalThis.fetch = async () => new Response("CHANGED", {{ status: 200, headers: {{ "content-length": "7" }} }});
let resp;
listeners.fetch({{
  request: new Request("/api/pdf?path=" + encodeURIComponent({json.dumps(path)})),
  respondWith: (p) => {{ resp = p; }}, waitUntil: () => {{}},
}});
const r = await resp;
console.log(JSON.stringify({{ status: r.status, body: await r.text() }}));
""")
    assert r["status"] == 200
    assert r["body"] == "%PDF-1.4 https://folio.test" + _key(path)   # the OLD cached bytes


# ---------------------------------------------------------------------------
# (g) SHELL_URLS: the two new wasm decoders are precached, and every /lib/
# entry actually exists (catches typos in the paths).
# ---------------------------------------------------------------------------

def test_shell_urls_include_the_new_wasm_decoders_and_every_lib_path_exists():
    shell = re.findall(r'"(/[^"]+)"',
                        slice_source(SW_JS.read_text(encoding="utf-8"),
                                     "const SHELL_URLS = [", "];"))
    assert "/lib/pdfjs/wasm/openjpeg.wasm" in shell
    assert "/lib/pdfjs/wasm/qcms_bg.wasm" in shell
    missing = [p for p in shell if p.startswith("/lib/") and not (STATIC / p.lstrip("/")).is_file()]
    assert missing == []
