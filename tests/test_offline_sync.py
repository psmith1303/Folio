"""Keeping setlists and Recent offline: cache.js syncOfflineSet and
requestPersistentStorage, run under Deno with the in-memory IndexedDB and
Cache Storage of test_offline_lru (FAKES) and api.js stubbed.

The sync asks /api/offline/wanted for every setlist path and the Recent
list, marks setlist PDFs held (pinned until no setlist holds them),
downloads what isn't cached, and bumps cached Recent items so they outrank
other auto-cached PDFs.
"""

import json

from deno_harness import requires_deno
from js_module_harness import MODULES, run_module
from test_offline_lru import FAKES, _key

pytestmark = requires_deno

SETUP = """
globalThis.window = globalThis; globalThis.isSecureContext = true;
console.info = () => {};   // Deno prints it to stdout, which carries the result
""" + FAKES + """
for (const b of ["libraryBody", "recentBody", "newestBody", "setlistSongsBody"]) {
  __el(b).querySelectorAll = () => [];
}
globalThis.__wanted = { setlist_paths: [], recent_paths: [] };
__returns.api = async (url) => {
  if (url !== "/api/offline/wanted") throw new Error("unexpected " + url);
  if (__wanted instanceof Error) throw __wanted;
  return JSON.parse(JSON.stringify(__wanted));
};
"""


def _sync(body: str, setup: str = ""):
    return run_module(MODULES / "cache.js", ["dom", "api"], body + "\nawait __tick();",
                      setup=SETUP + setup)


def _fetched_paths(r) -> list[str]:
    from urllib.parse import parse_qs, urlparse
    return [parse_qs(urlparse(u).query)["path"][0] for u in r["fetched"]]


def test_sync_holds_setlist_pdfs_and_caches_recent_ones():
    r = _sync("""
await __seedCached("/held-already.pdf", 50);
await __seedCached("/recent-cached.pdf", 200);
__wanted = { setlist_paths: ["/held-already.pdf", "/s1.pdf"],
             recent_paths: ["/r-new.pdf", "/recent-cached.pdf", "/r-old.pdf"] };
__setNow(5000);
await M.syncOfflineSet();
console.log(JSON.stringify({ ...__state(), fetched: __fetched,
  button: M.cacheButtonHtml("/s1.pdf"), status: await M.getCacheStatus() }));
""")
    # Only what wasn't cached is downloaded: setlist first, then Recent oldest-first.
    assert _fetched_paths(r) == ["/s1.pdf", "/r-old.pdf", "/r-new.pdf"]
    assert r["lru"]["/held-already.pdf"].get("held") is True
    assert r["lru"]["/s1.pdf"].get("held") is True
    assert r["lru"]["/s1.pdf"]["pinned"] is False          # held, not pinned by the user
    assert "held" not in r["lru"]["/r-new.pdf"]
    # Every Recent item is touched, oldest first, so they outrank other
    # auto-cached PDFs and keep their order.
    used = [r["lru"][p]["lastUsed"] for p in ["/r-old.pdf", "/recent-cached.pdf", "/r-new.pdf"]]
    assert used == sorted(used) and used[0] >= 5000
    assert "Pinned by a setlist" in r["button"]
    assert sorted(r["stores"]["folio-pdfs-v2"]) == sorted(
        _key(p) for p in ["/held-already.pdf", "/recent-cached.pdf", "/s1.pdf", "/r-old.pdf", "/r-new.pdf"])


def test_downloads_at_the_limit_evict_neither_setlist_nor_recent_pdfs():
    """At MAX_AUTO_CACHED, each download evicts the oldest auto-cached
    entry: that must be some other PDF, not a cached Recent item (oldest
    here) or a setlist PDF downloaded earlier in the run."""
    r = _sync("""
const L = self.FolioLru;
await __seedCached("/rc.pdf", 1);
for (let i = 1; i < L.MAX_AUTO_CACHED; i++) await __seedCached(`/u${i}.pdf`, 10 + i);
__wanted = { setlist_paths: ["/s1.pdf", "/s2.pdf"], recent_paths: ["/rn.pdf", "/rc.pdf"] };
await M.syncOfflineSet();
await __tick(50);
console.log(JSON.stringify(__state()));
""")
    lru = r["lru"]
    for p in ["/rc.pdf", "/rn.pdf", "/s1.pdf", "/s2.pdf"]:
        # The copy itself, not just an entry: a touch after an eviction can
        # leave an entry (size 0) with nothing cached behind it.
        assert _key(p) in r["stores"]["folio-pdfs-v2"], p
        assert p in lru and lru[p]["size"] > 0, p
    assert lru["/s1.pdf"].get("held") is True and lru["/s2.pdf"].get("held") is True
    assert sum(1 for e in lru.values() if not e["pinned"] and not e.get("held")) <= 500


def test_sync_releases_a_pdf_no_setlist_holds_any_more():
    r = _sync("""
await __seedCached("/gone.pdf", 50, { held: true });
await __seedCached("/both.pdf", 60, { held: true, pinned: true });
__wanted = { setlist_paths: [], recent_paths: [] };
await M.syncOfflineSet();
console.log(JSON.stringify(__state()));
""")
    assert r["lru"]["/gone.pdf"] == {"size": 1, "pinned": False, "lastUsed": 50}   # auto-cached now
    assert r["lru"]["/both.pdf"] == {"size": 1, "pinned": True, "lastUsed": 60}    # the user's pin stays


def test_a_failed_wanted_request_leaves_every_hold_alone():
    """Offline, or the server couldn't read the setlists (503): an empty
    list would release every hold, so nothing changes."""
    r = _sync("""
await __seedCached("/held.pdf", 50, { held: true });
__wanted = new Error("503: Setlists unreadable");
await M.syncOfflineSet();
console.log(JSON.stringify({ ...__state(), fetched: __fetched }));
""")
    assert r["lru"]["/held.pdf"].get("held") is True
    assert r["fetched"] == []


def _worker(reply: str) -> str:
    """A controlling service worker that answers GET_VERSION with *reply*
    (through api.js's askServiceWorker, stubbed)."""
    return f"""
Object.defineProperty(navigator, "serviceWorker", {{ configurable: true, value: {{ controller: {{}} }} }});
__returns.askServiceWorker = async (msg) => msg.type === "GET_VERSION" ? {reply} : null;
"""


def test_sync_is_skipped_under_an_old_service_worker():
    """A 2.16 worker answers without lruSchema: its store would drop held
    flags and evict what the sync downloads."""
    r = _sync("""
await __seedCached("/held.pdf", 50, { held: true });
__wanted = { setlist_paths: [], recent_paths: ["/r.pdf"] };
await M.syncOfflineSet();
console.log(JSON.stringify({ ...__state(), fetched: __fetched }));
""", setup=_worker('{ version: "2.16.1" }'))
    assert r["fetched"] == []
    assert r["lru"]["/held.pdf"].get("held") is True


def test_sync_runs_under_a_current_service_worker():
    r = _sync("""
__wanted = { setlist_paths: [], recent_paths: ["/r.pdf"] };
await M.syncOfflineSet();
console.log(JSON.stringify({ fetched: __fetched }));
""", setup=_worker('{ version: "x", lruSchema: self.FolioLru.LRU_SCHEMA }'))
    assert _fetched_paths(r) == ["/r.pdf"]


def test_a_missing_pdf_is_skipped_and_the_rest_still_sync():
    r = _sync("""
const realFetch = fetch;
globalThis.fetch = async (url) => url.includes("missing")
  ? (__fetched.push(url), new Response("no", { status: 404 }))
  : realFetch(url);
__wanted = { setlist_paths: ["/missing.pdf", "/s.pdf"], recent_paths: [] };
await M.syncOfflineSet();
console.log(JSON.stringify(__state()));
""")
    assert "/missing.pdf" not in r["lru"]
    assert r["lru"]["/s.pdf"].get("held") is True


def test_going_offline_mid_run_stops_but_marks_what_was_fetched():
    r = _sync("""
const realFetch = fetch;
let n = 0;
globalThis.fetch = async (url) => {
  if (++n > 1) { __fetched.push(url); throw new TypeError("Load failed"); }
  return realFetch(url);
};
__wanted = { setlist_paths: ["/s1.pdf", "/s2.pdf", "/s3.pdf"], recent_paths: [] };
await M.syncOfflineSet();
console.log(JSON.stringify({ ...__state(), fetched: __fetched }));
""")
    assert r["lru"]["/s1.pdf"].get("held") is True
    assert "/s2.pdf" not in r["lru"] and "/s3.pdf" not in r["lru"]
    assert len(r["fetched"]) == 2          # stopped at the first network failure


def test_a_call_during_a_run_queues_one_more_run():
    r = _sync("""
let calls = 0;
const orig = __returns.api;
__returns.api = async (url) => { calls++; return orig(url); };
__wanted = { setlist_paths: [], recent_paths: [] };
const a = M.syncOfflineSet();
const b = M.syncOfflineSet();
const c = M.syncOfflineSet();
await Promise.all([a, b, c]);
console.log(JSON.stringify({ calls, same: a === b && b === c }));
""")
    assert r == {"calls": 2, "same": True}


# ---------------------------------------------------------------------------
# Persistent storage
# ---------------------------------------------------------------------------


def _storage(persisted: str, persist: str) -> str:
    return f"""
globalThis.__asked = 0;
Object.defineProperty(navigator, "storage", {{ configurable: true, value: {{
  persisted: async () => {persisted},
  persist: async () => {{ __asked++; return {persist}; }},
}} }});
"""


def test_persistent_storage_is_requested_when_not_yet_granted():
    r = _sync("await M.requestPersistentStorage(); console.log(JSON.stringify(__asked));",
              setup=_storage("false", "true"))
    assert r == 1


def test_persistent_storage_is_not_requested_again():
    r = _sync("await M.requestPersistentStorage(); console.log(JSON.stringify(__asked));",
              setup=_storage("true", "true"))
    assert r == 0


def test_persistent_storage_failure_or_missing_api_is_silent():
    r = _sync("""
await M.requestPersistentStorage();
Object.defineProperty(navigator, "storage", { configurable: true, value: undefined });
await M.requestPersistentStorage();
console.log(JSON.stringify("ok"));
""", setup=_storage("false", "(() => { throw new Error('denied'); })()"))
    assert r == "ok"


# ---------------------------------------------------------------------------
# When this device changes a setlist (setlists.js, cache.js stubbed)
# ---------------------------------------------------------------------------

SETLIST_SIBLINGS = ["dom", "state", "api", "views", "viewer", "cache", "library-filter"]


def _setlists(body: str, state: dict) -> list[str]:
    """Run *body* against the real setlists.js; return the api calls' methods
    and the syncs, in order."""
    from test_score_tables import FAKE_STORAGE
    return run_module(MODULES / "setlists.js", SETLIST_SIBLINGS, body + """
await __tick();
console.log(JSON.stringify(__log.flatMap((e) => {
  if (e === "cache:syncOfflineSet") return ["sync"];
  if (!e.startsWith("api:api:")) return [];
  const [url, opts] = JSON.parse(e.slice("api:api:".length));
  return [(opts && opts.method || "GET") + " " + url];
})));
""", state=state, setup=FAKE_STORAGE + """
globalThis.confirm = () => true;
__returns.api = async (url, opts) => (opts ? {} : { items: [], setlists: [] });
""")


def test_adding_the_current_score_syncs_after_the_save():
    """Synced before the PUT lands, the server would still list the old
    setlist and the new song would stay unpinned until the next launch."""
    r = _setlists('await M.addCurrentScoreToSetlist("Gig", 1, null);',
                  {"currentScore": {"filepath": "a.pdf", "title": "A", "composer": "C"}})
    assert r == ["GET /api/setlists/Gig", "PUT /api/setlists/Gig", "sync"]


def test_saving_a_setlist_syncs_after_the_save():
    r = _setlists("""
M.initSetlistEvents();
__el("songPickerDialog").returnValue = "add";
__el("songStart").value = "1";
__el("songEnd").value = "";
// The song is saved, then the table re-rendered, which needs a real DOM.
try { __el("songPickerDialog").listeners.close(); }
catch (e) { if (!String(e).includes("is not a function")) throw e; }
""", {"editingSetlistName": "Gig", "editingSetlistItems": [],
      "pickerSelectedScore": {"filepath": "a.pdf", "title": "A", "composer": "C"}})
    assert r[:2] == ["PUT /api/setlists/Gig", "sync"]


def test_deleting_a_setlist_syncs_to_release_its_holds():
    r = _setlists("""
M.initSetlistEvents();
__el("btnDeleteSetlist").listeners.click();
""", {"editingSetlistName": "Gig", "editingSetlistItems": []})
    assert r[:2] == ["DELETE /api/setlists/Gig", "sync"]


def test_a_failed_save_does_not_sync():
    r = _setlists("""
__returns.api = async (url, opts) => { if (opts) throw new Error("500"); return { items: [] }; };
await M.addCurrentScoreToSetlist("Gig", 1, null);
""", {"currentScore": {"filepath": "a.pdf", "title": "A", "composer": "C"}})
    assert "sync" not in r
