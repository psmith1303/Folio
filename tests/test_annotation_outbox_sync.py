"""syncEntry / mergedWithServer / isOffline (annot-outbox.js): pushing a
pending offline entry back to the server, merging with whatever changed
there meanwhile, and telling a dropped connection apart from a real server
error.

Runs the REAL annot-outbox.js module under Deno (js_module_harness), with
api.js stubbed by a small in-process fake server (etag concurrency, an
"offline" switch that throws like a real dropped fetch or the service
worker's cached-GET 503) and a fake IndexedDB backing the "pending" store.
"""

import json
from pathlib import Path

import pytest

from deno_harness import requires_deno
from js_module_harness import MODULES, run_module

pytestmark = requires_deno

OUTBOX_JS = MODULES / "annot-outbox.js"

# ---------------------------------------------------------------------------
# Fakes shared by every test in this file
# ---------------------------------------------------------------------------

# A minimal IndexedDB backing one object store ("pending", keyPath "path"):
# open(name, version) with onupgradeneeded/createObjectStore, a transaction
# that supports exactly one get/getAll/put/delete request (all this code
# ever issues per transaction), tx.oncomplete, and db.onversionchange as a
# plain assignable property.
IDB_FAKE = r"""
globalThis.__idb = new Map();       // path -> entry: the "pending" store
let __idbCreated = false;
function __tx() {
  let action = null;
  const t = { oncomplete: null, onerror: null };
  t.objectStore = () => ({
    get(k) { const req = {}; action = () => { req.result = __idb.get(k); }; return req; },
    getAll() { const req = {}; action = () => { req.result = [...__idb.values()]; }; return req; },
    put(v) { const req = {}; action = () => { __idb.set(v.path, v); req.result = v.path; }; return req; },
    delete(k) { const req = {}; action = () => { __idb.delete(k); }; return req; },
  });
  queueMicrotask(() => { if (action) action(); if (t.oncomplete) t.oncomplete(); });
  return t;
}
globalThis.indexedDB = {
  open() {
    const req = {};
    queueMicrotask(() => {
      const db = { transaction: () => __tx(), close() {} };
      Object.defineProperty(db, "onversionchange", { set(fn) { db.__vc = fn; }, get() { return db.__vc; } });
      if (!__idbCreated) {
        __idbCreated = true;
        req.result = db;
        db.createObjectStore = () => {};
        if (req.onupgradeneeded) req.onupgradeneeded();
      }
      req.result = db;
      if (req.onsuccess) req.onsuccess();
    });
    return req;
  },
};
"""

# A fake server: one sidecar (pages/rotations/etag) with etag concurrency
# matching web/core.py's save_annotations, plus an offline switch. Offline,
# a GET throws like the service worker's cached-GET 503 answer (an Error
# whose message starts "503"), and a PUT throws a real TypeError (a dropped
# fetch()), matching how api() reports each.
API_FAKE = r"""
globalThis.__calls = [];
globalThis.__online = true;
globalThis.__server = { pages: {}, rotations: {}, etag: "" };
let __etagN = 0;
globalThis.__setOnline = (v) => { __online = v; };
globalThis.__setServer = (s) => { __server = s; };
async function api(url, opts = {}) {
  const method = (opts && opts.method) || "GET";
  __calls.push({ method, body: opts.body ? JSON.parse(opts.body) : undefined });
  if (!__online) {
    if (method === "GET") throw new Error("503: offline");
    throw new TypeError("Failed to fetch");
  }
  if (method === "GET") {
    return { pages: __server.pages, rotations: __server.rotations, etag: __server.etag };
  }
  if (method === "PUT") {
    const body = JSON.parse(opts.body);
    const expected = body.expected_etag;
    if (expected !== null && expected !== undefined && expected !== __server.etag) {
      throw new Error("409: conflict");
    }
    __server = { pages: body.pages, rotations: body.rotations, etag: "e" + (++__etagN) };
    return { etag: __server.etag };
  }
  throw new Error(`404: no route for ${method} ${url}`);
}
"""


def _run(body: str, *, api_returns: bool = True) -> dict:
    """Run *body* against the real module, with the fakes above wired in via
    the api.js stub's __returns hook (js_module_harness generates an "api"
    stub that calls __returns.api(...) when set)."""
    setup = IDB_FAKE + API_FAKE + "globalThis.__returns = { api };\n"
    return run_module(OUTBOX_JS, ["api"], body, setup=setup)


def _entry(path="/m/A.pdf", base=None, base_etag="", pages=None, rotations=None) -> dict:
    return {
        "path": path,
        "base": base if base is not None else {"pages": {}, "rotations": {}},
        "baseEtag": base_etag,
        "pages": pages if pages is not None else {"0": [{"uuid": "a", "type": "ink"}]},
        "rotations": rotations if rotations is not None else {},
    }


# ---------------------------------------------------------------------------
# isOffline
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expr, expected", [
    ('new TypeError("Failed to fetch")', True),
    ('new Error("503: offline")', True),
    ('new Error("503")', True),
    ('new Error("409: conflict")', False),
    ('new Error("404: not found")', False),
    ('new Error("some other message")', False),
    ("undefined", False),
])
def test_is_offline(expr, expected):
    r = _run(f"console.log(JSON.stringify(M.isOffline({expr})));", api_returns=False)
    assert r is expected


# ---------------------------------------------------------------------------
# syncEntry: the happy and unhappy paths
# ---------------------------------------------------------------------------

def test_no_conflict_sends_one_put_with_the_known_base_etag():
    r = _run(f"""
__setServer({{ pages: {{}}, rotations: {{}}, etag: "e0" }});
const entry = {json.dumps(_entry(base_etag="e0"))};
const result = await M.syncEntry(entry);
console.log(JSON.stringify({{ result, calls: __calls, pending: [...__idb.keys()] }}));
""")
    assert r["result"]["pages"] == {"0": [{"uuid": "a", "type": "ink"}]}
    assert [c["method"] for c in r["calls"]] == ["PUT"]
    assert r["calls"][0]["body"]["expected_etag"] == "e0"
    assert r["pending"] == []          # entry removed on success


def test_conflict_then_success_merges_with_server_and_retries():
    r = _run(f"""
__setServer({{ pages: {{ "1": [{{ uuid: "r", type: "ink" }}] }}, rotations: {{}}, etag: "e-server" }});
const entry = {json.dumps(_entry(base_etag="e-stale"))};
const result = await M.syncEntry(entry);
console.log(JSON.stringify({{ result, calls: __calls.map((c) => c.method) }}));
""")
    # first PUT sent with the stale etag conflicts; syncEntry then GETs the
    # server's real state, merges, and retries the PUT, which succeeds.
    assert r["calls"] == ["PUT", "GET", "PUT"]
    # both the local addition and the server's own addition survive the merge
    all_uuids = {a["uuid"] for page in r["result"]["pages"].values() for a in page}
    assert all_uuids == {"a", "r"}


def test_conflict_every_time_throws_after_three_attempts_and_keeps_the_entry():
    r = _run(f"""
const entry = {json.dumps(_entry(base_etag="e-stale"))};
await M.putPending(entry);
globalThis.__returns.api = async (url, opts) => {{
  const method = (opts && opts.method) || "GET";
  __calls.push({{ method, body: opts && opts.body ? JSON.parse(opts.body) : undefined }});
  if (method === "GET") return {{ pages: {{}}, rotations: {{}}, etag: "e-server" }};
  throw new Error("409: conflict");        // every PUT conflicts, forever
}};
__calls.length = 0;
let threw = null;
try {{ await M.syncEntry(entry); }} catch (e) {{ threw = e.message; }}
console.log(JSON.stringify({{
  threw, pending: [...__idb.keys()],
  putCalls: __calls.filter((c) => c.method === "PUT").length,
  getCalls: __calls.filter((c) => c.method === "GET").length,
}}));
""")
    assert r["threw"] is not None
    assert r["pending"] == ["/m/A.pdf"]     # kept, not lost
    assert r["putCalls"] == 3               # 3 attempts, per syncEntry's retry cap
    assert r["getCalls"] == 3               # syncEntry re-merges with the server after each conflict


def test_offline_at_put_returns_null_and_keeps_the_entry():
    r = _run(f"""
__setServer({{ pages: {{}}, rotations: {{}}, etag: "e0" }});
const entry = {json.dumps(_entry(base_etag="e0"))};
await M.putPending(entry);
__setOnline(false);
const result = await M.syncEntry(entry);
console.log(JSON.stringify({{ result, pending: [...__idb.keys()] }}));
""")
    assert r["result"] is None
    assert r["pending"] == ["/m/A.pdf"]


@pytest.mark.parametrize("offline_kind", ["service_worker_503", "real_typeerror"])
def test_offline_at_the_pre_merge_get_returns_null_and_keeps_the_entry(offline_kind):
    override = "" if offline_kind == "service_worker_503" else """
globalThis.__returns.api = async (url, opts) => {
  const method = (opts && opts.method) || "GET";
  if (method === "GET") throw new TypeError("Failed to fetch");
  throw new Error("should not PUT while offline");
};
"""
    r = _run(f"""
{override}
const entry = {json.dumps(_entry(base_etag=None))};    // unknown base: merge-first
await M.putPending(entry);
__setOnline({"false" if offline_kind == "service_worker_503" else "true"});
const result = await M.syncEntry(entry);
console.log(JSON.stringify({{ result, pending: [...__idb.keys()] }}));
""")
    assert r["result"] is None
    assert r["pending"] == ["/m/A.pdf"]


def test_unknown_base_merges_with_the_server_before_the_first_put():
    """A score opened offline with nothing cached has baseEtag null: syncEntry
    must fetch the server's real state and merge before ever sending a PUT,
    and the PUT it does send must carry a real expected_etag, never a
    missing one (which the server would treat as an unconditional overwrite,
    silently discarding whatever another device wrote)."""
    r = _run(f"""
__setServer({{ pages: {{ "2": [{{ uuid: "other-device", type: "ink" }}] }}, rotations: {{}}, etag: "e-real" }});
const entry = {json.dumps(_entry(base_etag=None))};
const result = await M.syncEntry(entry);
console.log(JSON.stringify({{ result, calls: __calls }}));
""")
    assert [c["method"] for c in r["calls"]] == ["GET", "PUT"]
    put_body = r["calls"][1]["body"]
    assert "expected_etag" in put_body and put_body["expected_etag"] is not None
    assert put_body["expected_etag"] == "e-real"
    # the other device's own addition was not overwritten/lost
    all_uuids = {a["uuid"] for page in r["result"]["pages"].values() for a in page}
    assert "other-device" in all_uuids
    assert "a" in all_uuids


def test_other_http_errors_throw_and_keep_the_entry():
    r = _run(f"""
const entry = {json.dumps(_entry(base_etag="e0"))};
await M.putPending(entry);
globalThis.__returns.api = async () => {{ throw new Error("404: score gone"); }};
let threw = null;
try {{ await M.syncEntry(entry); }} catch (e) {{ threw = e.message; }}
console.log(JSON.stringify({{ threw, pending: [...__idb.keys()] }}));
""")
    assert r["threw"] == "404: score gone"
    assert r["pending"] == ["/m/A.pdf"]


# ---------------------------------------------------------------------------
# The pending store itself (getPending/putPending/deletePending/allPending)
# ---------------------------------------------------------------------------

def test_pending_store_roundtrip():
    r = _run(f"""
const missing = await M.getPending("/m/A.pdf");
await M.putPending({json.dumps(_entry())});
const got = await M.getPending("/m/A.pdf");
const all1 = await M.allPending();
await M.deletePending("/m/A.pdf");
const all2 = await M.allPending();
console.log(JSON.stringify({{ missing: missing ?? null, got, all1, all2 }}));
""")
    assert r["missing"] is None
    assert r["got"]["path"] == "/m/A.pdf"
    assert [e["path"] for e in r["all1"]] == ["/m/A.pdf"]
    assert r["all2"] == []
