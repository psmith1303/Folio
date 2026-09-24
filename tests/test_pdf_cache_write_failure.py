"""A failed cache write mustn't turn a good PDF download into an error.

On a cache miss the service worker downloads the PDF and stores it for
offline use before answering. When storing threw (QuotaExceededError on a
full iPad, or an IndexedDB failure recording it), the error reached the
fetch handler's "offline" catch: the viewer got 503 "Offline — PDF not
cached" although the server had just sent the PDF. Runs the REAL sw.js
under Deno, with the fakes and boot from test_offline_lru.py.
"""

import pytest

from deno_harness import requires_deno
from test_offline_lru import _sw

pytestmark = requires_deno

# The first PDF request, answered by the service worker's fetch handler,
# with the cache write or the LRU record made to throw.
BODY = """
const failing = %(failing)s;
let putAttempts = 0;
const open = caches.open;
caches.open = async (name) => {
  const c = await open(name);
  if (failing === "put") {
    c.put = async () => { putAttempts++; throw new DOMException("quota", "QuotaExceededError"); };
  }
  return c;
};
if (failing === "lru") {
  indexedDB.open = () => { const r = {}; setTimeout(() => { r.error = new Error("IDB broken"); r.onerror?.(); }, 0); return r; };
}
let resp;
listeners.fetch({
  request: new Request("/api/pdf?path=" + encodeURIComponent("jazz/Davis - Blue.pdf")),
  respondWith: (p) => { resp = p; }, waitUntil: () => {},
});
const r = await resp;
console.log(JSON.stringify({ status: r.status, body: await r.text(), putAttempts,
  fetched: __fetched.length }));
"""


@pytest.mark.parametrize("failing", ["put", "lru"])
def test_a_failed_cache_write_still_returns_the_pdf(failing):
    r = _sw(BODY % {"failing": repr(failing)})
    assert r["fetched"] == 1                        # it did download it
    if failing == "put":
        assert r["putAttempts"] == 1                # ...and tried to store it
    assert r["status"] == 200
    assert r["body"].startswith("%PDF-1.4 ")
