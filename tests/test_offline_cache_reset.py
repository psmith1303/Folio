"""Offline copies start fresh when paths become library-relative (Step 10b).

The v1 PDF cache, API cache and "folio-lru" IndexedDB were keyed by
absolute paths, which the page no longer asks for. The user chose to drop
them rather than re-key them: the new service worker uses -v2 names and, on
activate, deletes the v1 caches and database. Runs the REAL sw.js under
Deno, with the fakes and boot from test_offline_lru.py.
"""

import re

from deno_harness import requires_deno
from test_offline_lru import SW_JS, _sw

pytestmark = requires_deno

SHELL = "folio-v" + re.search(r'const APP_VERSION = "([^"]+)"',
                              SW_JS.read_text(encoding="utf-8")).group(1)

# Record the IndexedDB databases opened and deleted (the fake ignores names).
RECORD_DBS = """
const __opened = new Set(), __deleted = [];
const __open = indexedDB.open;
indexedDB.open = (name, v) => { __opened.add(name + "@" + v); return __open(name, v); };
indexedDB.deleteDatabase = (name) => { __deleted.push(name); return {}; };
const activate = async () => { let w; listeners.activate({ waitUntil: (p) => { w = p; } }); await w; };
"""


def test_activate_drops_the_absolute_keyed_caches_and_database():
    r = _sw(RECORD_DBS + """
for (const n of ["folio-pdfs-v1", "folio-api-v1", "folio-v2.13.10"]) {
  await (await caches.open(n)).put("/api/pdf?path=%2Fmnt%2Fz%2Fa.pdf", new Response("old"));
}
await install();
await activate();
console.log(JSON.stringify({ caches: Object.keys(__state().stores).sort(), deleted: __deleted }));
""")
    assert r["caches"] == [SHELL]
    assert r["deleted"] == ["folio-lru"]


def test_activate_keeps_the_current_caches():
    """The sweep must not take the new generation with it."""
    r = _sw(RECORD_DBS + """
await install();
await fetchPdf("jazz/Davis - Blue.pdf");
await (await caches.open("folio-api-v2")).put("/api/library", new Response("{}"));
await activate();
console.log(JSON.stringify(__state()));
""")
    assert sorted(r["stores"]) == sorted([SHELL, "folio-api-v2", "folio-pdfs-v2"])
    assert r["stores"]["folio-pdfs-v2"] == ["/api/pdf?path=jazz%2FDavis%20-%20Blue.pdf"]
    assert list(r["lru"]) == ["jazz/Davis - Blue.pdf"]


def test_offline_copies_are_recorded_in_the_new_database_only():
    r = _sw(RECORD_DBS + """
await install();
await activate();
await fetchPdf("Bach - Suite.pdf");
console.log(JSON.stringify({ opened: [...__opened] }));
""")
    assert r["opened"] == ["folio-lru-v2@1"]
