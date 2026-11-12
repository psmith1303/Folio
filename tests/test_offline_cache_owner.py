"""offline-lru.js is the only code that writes the PDF cache or the LRU store.

A cached PDF (in Cache Storage, under PDF_CACHE) and its LRU entry (in the
"folio-lru-v2" IndexedDB store) must change together. When other code wrote
either one, they drifted: the service worker's touch after a cache hit
created an entry for a PDF just evicted (size 0, nothing behind it), and
cache.js deleted PDF_CACHE itself and left the entries behind.

Hermetic source checks (no Deno needed); test_offline_lru.py runs the
behaviour itself.
"""

import re
from pathlib import Path

import pytest

from deno_harness import slice_source

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
OWNER = STATIC / "modules" / "offline-lru.js"
SW_JS = STATIC / "sw.js"

# Every shipped script but the owner (pdf.js is vendored, under lib/).
OTHERS = sorted(
    p for p in [*STATIC.glob("*.js"), *(STATIC / "modules").glob("*.js")]
    if p != OWNER
)


def _rel(p: Path) -> str:
    return p.relative_to(STATIC).as_posix()


def test_the_scripts_checked_include_the_page_and_the_worker():
    """A glob that matched nothing would pass every check below."""
    names = {_rel(p) for p in OTHERS}
    assert {"sw.js", "app.js", "modules/cache.js"} <= names


@pytest.mark.parametrize("path", OTHERS, ids=_rel)
def test_no_other_script_deletes_the_pdf_cache_or_opens_the_lru_store(path):
    """cache.js used to run caches.delete(PDF_CACHE) itself; clearing goes
    through clearAllPdfs, which clears the entries with the copies."""
    src = path.read_text(encoding="utf-8")
    assert "caches.delete(PDF_CACHE)" not in src
    assert '"folio-pdfs-v2"' not in src     # the name, spelled out
    assert '"folio-lru-v2"' not in src      # the store is opened only by its owner
    assert not re.search(r"\bLRU_DB\b", src)  # sw.js's OLD_LRU_DB is v1's


@pytest.mark.parametrize("path", OTHERS, ids=_rel)
def test_no_other_script_opens_the_pdf_cache_to_write(path):
    """Only the service worker opens PDF_CACHE outside the owner, to serve a
    PDF (handlePdfFetch); there it may only read. Storing goes through
    storePdf, which records the entry with the copy."""
    src = path.read_text(encoding="utf-8")
    if path != SW_JS:
        assert "caches.open(PDF_CACHE)" not in src
        return
    assert src.count("caches.open(PDF_CACHE)") == 1
    body = slice_source(src, "async function handlePdfFetch(request) {", "\n}\n")
    assert "caches.open(PDF_CACHE)" in body
    uses = set(re.findall(r"\bcache\.(\w+)\(", body))
    assert uses == {"match"}


def test_a_cache_hit_notes_the_use_and_cannot_create_an_entry():
    """The worker's touch after a hit created a missing entry; noteUsed
    updates an existing one only. The entry-creating touch is gone from
    every script, and the owner keeps entry creation to itself."""
    for p in OTHERS:
        assert "touchLruEntry" not in p.read_text(encoding="utf-8"), _rel(p)
    body = slice_source(SW_JS.read_text(encoding="utf-8"),
                        "async function handlePdfFetch(request) {", "\n}\n")
    assert "noteUsed(pdfPath)" in body
    owner = OWNER.read_text(encoding="utf-8")
    exports = slice_source(owner, "  return {\n    PDF_CACHE,", "\n  };\n")
    assert "noteUsed" in exports
    assert "recordEntry" not in exports
    assert "touchLruEntry" not in owner
