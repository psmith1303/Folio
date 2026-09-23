"""The page and the service worker must agree on a PDF's Cache Storage key.

cache.js (pin, evict, "Cache setlist", purging a corrupt copy) and sw.js
(serving and storing PDFs) each build the key; if the page's key differs,
or throws, the page can't find or remove what the service worker cached.
Both real functions are sliced out of the shipped files and run under Deno.
"""

import json
from pathlib import Path

import pytest

from deno_harness import requires_deno, run_deno, slice_source

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
CACHE_JS = STATIC / "modules" / "cache.js"
SW_JS = STATIC / "sw.js"

PATHS = [
    "/mnt/z/PARA/Resources/Music/Bach - Suite -- a b.pdf",
    "/m/Dvořák & Sons #1?.pdf",          # non-ASCII and URL metacharacters
    "/m/100% ok+.pdf",                   # characters that decode differently
    "/m/jazz/Davis - Blue.pdf",
]


def _keys(paths: list[str]) -> list[dict]:
    """For each path: the page's key, and the key sw.js derives from the
    URL the page fetches (the key itself, resolved against the origin)."""
    page_fn = slice_source(CACHE_JS.read_text(encoding="utf-8"),
                           "export function pdfCacheKey(", "\n")
    sw_fn = slice_source(SW_JS.read_text(encoding="utf-8"),
                         "function pdfCacheKey(url) {", "\n}\n") + "\n}\n"
    # Both files name the function pdfCacheKey: keep the page's in its own
    # scope so any call it makes resolves as it does in the browser.
    page = "const pageKey = (() => {\n" + page_fn.replace("export ", "", 1) \
        + "\nreturn pdfCacheKey;\n})();\n"
    return run_deno(
        page + sw_fn + f"""
console.log(JSON.stringify({json.dumps(paths)}.map(p => {{
  const page = pageKey(p);
  return {{ page, sw: pdfCacheKey(new URL(page, "https://x.test").href) }};
}})));
""")


@requires_deno
@pytest.mark.parametrize("path", PATHS)
def test_page_and_service_worker_keys_match(path):
    """Regression: pdfCacheKey called itself, so every call threw
    'Maximum call stack size exceeded' and caching, pinning, eviction and
    corrupt-copy purging all failed."""
    [k] = _keys([path])
    assert k["page"] == k["sw"]
    assert k["page"].startswith("/api/pdf?path=")


def test_page_key_does_not_call_itself():
    """Hermetic guard (no Deno needed) for the same regression."""
    body = slice_source(CACHE_JS.read_text(encoding="utf-8"),
                        "export function pdfCacheKey(", "\n")
    assert "pdfCacheKey(" not in body.split("{", 1)[1]
