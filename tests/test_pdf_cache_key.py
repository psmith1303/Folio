"""The page and the service worker must agree on a PDF's Cache Storage key.

cache.js (pin, evict, "Cache setlist", purging a corrupt copy) and sw.js
(serving and storing PDFs) both use the key; if the page's key differs, or
throws, the page can't find or remove what the service worker cached. Both
now take it from the shared offline-lru.js (self.FolioLru.pdfCacheKey):
the page through cache.js's re-export, the service worker from the path
getPathFromPdfUrl reads out of the request URL (as handlePdfFetch does).
Run under Deno as each context loads it.
"""

import json
from pathlib import Path

import pytest

from deno_harness import requires_deno, slice_source
from js_module_harness import MODULES, run_module

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
SW_JS = STATIC / "sw.js"
LRU_JS = MODULES / "offline-lru.js"

PATHS = [
    "/mnt/z/psDATA/Resources/Music/Bach - Suite -- a b.pdf",
    "/m/Dvořák & Sons #1?.pdf",          # non-ASCII and URL metacharacters
    "/m/100% ok+.pdf",                   # characters that decode differently
    "/m/jazz/Davis - Blue.pdf",
]


def _keys(paths: list[str]) -> list[dict]:
    """For each path: the page's key (cache.js), and the key sw.js derives
    from the URL the page fetches (the key itself, resolved on the origin)."""
    sw_src = SW_JS.read_text(encoding="utf-8")
    get_path = slice_source(sw_src, "function getPathFromPdfUrl(url) {", "\n}\n") + "\n}\n"
    return run_module(MODULES / "cache.js", ["dom"], f"""
// The service worker's key for a PDF request: the path read from its URL,
// through the same self.FolioLru the page loaded.
{get_path}
const swKey = (url) => self.FolioLru.pdfCacheKey(getPathFromPdfUrl(url));
console.log(JSON.stringify({json.dumps(paths)}.map((p) => {{
  const page = M.pdfCacheKey(p);
  return {{ page, sw: swKey(new URL(page, "https://x.test").href) }};
}})));
""", setup="globalThis.window = globalThis; globalThis.isSecureContext = true;")


@requires_deno
@pytest.mark.parametrize("path", PATHS)
def test_page_and_service_worker_keys_match(path):
    """Regression: pdfCacheKey called itself, so every call threw
    'Maximum call stack size exceeded' and caching, pinning, eviction and
    corrupt-copy purging all failed."""
    [k] = _keys([path])
    assert k["page"] == k["sw"]
    assert k["page"].startswith("/api/pdf?path=")


def test_shared_key_does_not_call_itself():
    """Hermetic guard (no Deno needed) for the same regression."""
    body = slice_source(LRU_JS.read_text(encoding="utf-8"),
                        "function pdfCacheKey(path) {", "\n  }\n")
    assert "pdfCacheKey(" not in body.split("{", 1)[1]


def test_service_worker_keys_requests_by_their_path():
    """handlePdfFetch derives the key from the request's path, as the test
    above does; a hand-built key would drift from the shared format."""
    src = SW_JS.read_text(encoding="utf-8")
    assert "const cacheKey = pdfCacheKey(pdfPath);" in slice_source(
        src, "async function handlePdfFetch(request) {", "\n}\n")
    assert '"/api/pdf?path="' not in src


def test_service_worker_loads_the_shared_script_by_a_versioned_url():
    """An unversioned importScripts URL can be served stale from the HTTP
    cache when a new service worker installs, mixing old and new code."""
    src = SW_JS.read_text(encoding="utf-8")
    assert 'importScripts("/modules/offline-lru.js?v=" + APP_VERSION);' in src
    assert src.index("const APP_VERSION") < src.index("importScripts(")
