"""Offline annotation saves (annotations.js + the real annot-outbox.js).

The user's bug: annotation saves are PUT /api/annotations (whole state); the
service worker passes PUTs through, so offline they fail; the failure was
only logged, and reopening the score offline served the service worker's
cached (pre-edit) GET, so offline strokes were lost.

The fix keeps a failed save on the device (IndexedDB, annot-outbox.js),
shows it on reopening, and syncs it later with a three-way merge against
whatever the server has by then. These tests run annotations.js for real
(js_module_harness.run_module) with dom/utils/stamps/dialog-handlers/viewer
stubbed and state.js's __state pointed at a plain object we control; api.js
is stubbed by a small fake server, and annot-outbox.js (also real) is backed
by a fake IndexedDB. Three review-round regressions are guarded here too
(each was fixed just before this diff and is certified by mutation, per
tests/test_offline_annotations.py's counterpart section in the /test report):
(a) isOffline missing the service worker's 503, (b) a background sync racing
the open score's stale etag into a false conflict dialog, (c) a null etag
being sent (or skipped) as an unconditional overwrite.
"""

import json
from pathlib import Path

import pytest

from deno_harness import requires_deno
from js_module_harness import MODULES, run_module

pytestmark = requires_deno

ANNOTATIONS_JS = MODULES / "annotations.js"
FLOW_STUB = ["dom", "state", "api", "utils", "stamps", "dialog-handlers", "viewer"]

# ---------------------------------------------------------------------------
# Fakes: IndexedDB backing the "pending" store, and a fake annotations server
# ---------------------------------------------------------------------------

IDB_FAKE = r"""
globalThis.__idb = new Map();
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

# One sidecar PER PATH (a real server keeps one per score, with its own
# etag — a shared single etag would make independent scores conflict with
# each other), with etag concurrency matching web/core.py's
# save_annotations, and an offline switch: a GET offline throws like the
# service worker's cached-GET 503 answer; a PUT offline throws a real
# TypeError (dropped fetch()) — matching how api() reports each.
API_FAKE = r"""
globalThis.__calls = [];
globalThis.__online = true;
globalThis.__servers = new Map();
let __etagN = 0;
globalThis.__setOnline = (v) => { __online = v; };
globalThis.__setServer = (path, s) => { __servers.set(path, s); };
function __srv(path) {
  if (!__servers.has(path)) __servers.set(path, { pages: {}, rotations: {}, etag: "" });
  return __servers.get(path);
}
async function __api(url, opts = {}) {
  await new Promise((r) => setTimeout(r, 5));   // a little real-world latency
  const method = (opts && opts.method) || "GET";
  const body = opts && opts.body ? JSON.parse(opts.body) : undefined;
  __calls.push({ method, body, url });
  if (!__online) {
    if (method === "GET") throw new Error("503: offline");
    throw new TypeError("Failed to fetch");
  }
  if (method === "GET") {
    const path = new URL(url, "http://x").searchParams.get("path");
    const s = __srv(path);
    return { pages: s.pages, rotations: s.rotations, etag: s.etag };
  }
  if (method === "PUT") {
    const s = __srv(body.path);
    if (Object.prototype.hasOwnProperty.call(body, "expected_etag") &&
        body.expected_etag !== null && body.expected_etag !== s.etag) {
      throw new Error("409: conflict");
    }
    const newEtag = "e" + (++__etagN);
    __servers.set(body.path, { pages: body.pages, rotations: body.rotations, etag: newEtag });
    return { etag: newEtag };
  }
  throw new Error(`404: no route for ${method} ${url}`);
}
globalThis.__errors = [];
console.error = (...args) => {
  __errors.push(args.map((a) => (a && a.message) || String(a)).join(" "));
};
const __settleMs = 60;
globalThis.__settle = () => new Promise((r) => setTimeout(r, __settleMs));
"""

SETUP = IDB_FAKE + API_FAKE + "globalThis.__returns = { api: __api };\n"


OUTBOX_IMPORT = f'const O = await import("{(MODULES / "annot-outbox.js").as_uri()}");\n'


def _flow(body: str, state: dict) -> dict:
    return run_module(ANNOTATIONS_JS, FLOW_STUB, OUTBOX_IMPORT + body, state=state, setup=SETUP)


def _base_state(**overrides) -> dict:
    s = {
        "currentScore": {"filepath": "/m/A.pdf"},
        "annotations": {}, "rotations": {},
        "annotationEtag": "", "annotationBase": {"pages": {}, "rotations": {}},
        "annotationsOffline": False, "undoStacks": {},
    }
    s.update(overrides)
    return s


def _toasts(log):
    return [l for l in log if l.startswith("viewer:showToast")]


def _dialogs(log):
    return [l for l in log if l.startswith("dialog-handlers:showConflictDialog")]


# ---------------------------------------------------------------------------
# 1. The user's bug, end to end
# ---------------------------------------------------------------------------

def test_offline_strokes_survive_reopening_and_sync_once_back_online():
    r = _flow("""
// A stroke drawn offline: the PUT fails (dropped fetch), so it's kept.
__setOnline(false);
__state.annotations["0"] = [{ uuid: "s1", type: "ink" }];
await M.saveAnnotations();
await __settle();
const afterFirstStroke = {
  toasts: __log.filter((l) => l.startsWith("viewer:showToast")).length,
  offlineFlag: __state.annotationsOffline,
  pending: [...__idb.keys()],
};

// A second offline stroke must not toast again.
__state.annotations["0"].push({ uuid: "s2", type: "ink" });
await M.saveAnnotations();
await __settle();
const afterSecondStroke = { toasts: __log.filter((l) => l.startsWith("viewer:showToast")).length };

// Close and reopen offline: a fresh load reads the pending entry (which wins
// over the service worker's stale cached GET) and shows the offline strokes.
const pending = await M.pendingAnnotations("/m/A.pdf");
const staleCachedGet = { pages: {}, rotations: {}, etag: "" };   // pre-edit, from the SW cache
const shown = M.annotationState(staleCachedGet, pending);

// Apply what viewer.js's loadAndRenderPdf would apply, then go back online
// and sync (as openScore does when annotationState says there are pending edits).
__state.annotations = shown.pages;
__state.rotations = shown.rotations;
__state.annotationEtag = shown.etag;
__state.annotationBase = shown.base;
__setOnline(true);
await M.saveAnnotations();
await __settle();

console.log(JSON.stringify({
  afterFirstStroke, afterSecondStroke,
  shownPending: shown.pending, shownUuids: shown.pages["0"].map((a) => a.uuid),
  finalPending: [...__idb.keys()],
  finalEtag: __state.annotationEtag,
  finalUuids: (__state.annotations["0"] || []).map((a) => a.uuid),
  finalOfflineFlag: __state.annotationsOffline,
  toastMessages: __log.filter((l) => l.startsWith("viewer:showToast")),
}));
""", _base_state())

    assert r["afterFirstStroke"]["toasts"] == 1, "the offline toast must fire, once"
    assert r["afterFirstStroke"]["offlineFlag"] is True
    assert r["afterFirstStroke"]["pending"] == ["/m/A.pdf"]
    assert r["afterSecondStroke"]["toasts"] == 1, "a second offline stroke must not toast again"

    assert r["shownPending"] is True
    assert r["shownUuids"] == ["s1", "s2"], "reopening offline must show the offline strokes, not the stale cached GET"

    assert r["finalPending"] == [], "synced entry must be removed once back online"
    assert r["finalUuids"] == ["s1", "s2"], "no strokes lost across the offline→online sync"
    assert r["finalEtag"] and r["finalEtag"] != ""      # a real server etag was adopted
    assert r["finalOfflineFlag"] is False
    assert any("Offline annotations saved" in m for m in r["toastMessages"])
    assert sum("Offline annotations saved" in m for m in r["toastMessages"]) == 1


# ---------------------------------------------------------------------------
# 4. applySynced keeps strokes drawn while the sync was in flight
# ---------------------------------------------------------------------------

def test_a_stroke_drawn_during_the_sync_is_kept_and_saved_again():
    r = _flow("""
__setOnline(false);
__state.annotations["0"] = [{ uuid: "s1", type: "ink" }];
await M.saveAnnotations();
await __settle();            // s1 is now in the outbox

// Gate the next PUT so we can add a stroke while it's "in flight".
let release;
const gate = new Promise((r) => { release = r; });
const real = __api;
__returns.api = async (url, opts) => {
  if ((opts && opts.method) === "PUT") await gate;
  return real(url, opts);
};

__setOnline(true);
M.saveAnnotations();         // fires syncOpenScore; its PUT is gated
await new Promise((r) => setTimeout(r, 0));
__state.annotations["0"].push({ uuid: "s2", type: "ink" });   // drawn mid-sync
release();
await __settle();
await __settle();            // let the follow-up save (for s2) complete too

console.log(JSON.stringify({
  finalUuids: (__state.annotations["0"] || []).map((a) => a.uuid),
  pending: [...__idb.keys()],
  puts: __calls.filter((c) => c.method === "PUT").map((c) => c.body.pages["0"].map((a) => a.uuid)),
}));
""", _base_state())
    assert r["finalUuids"] == ["s1", "s2"], "the mid-sync stroke must survive the merge"
    assert r["pending"] == []
    # the sync's own PUT only carried s1 (what was sent); a follow-up save
    # then pushes s2 as well, so neither stroke is lost.
    assert r["puts"][0] == ["s1"]
    assert r["puts"][-1] == ["s1", "s2"] or "s2" in r["puts"][-1]


# ---------------------------------------------------------------------------
# Review fix (a): isOffline must recognize the service worker's 503, not
# just a real dropped fetch — or an offline stroke with an unknown base
# logs "Failed to sync" instead of toasting.
# ---------------------------------------------------------------------------

def test_offline_stroke_with_unknown_base_toasts_and_does_not_log_an_error():
    """Regression: a score opened offline with nothing cached has baseEtag
    null, so syncEntry premerges with the server first — a GET, which the
    service worker answers 503 while offline (not a TypeError). If isOffline
    doesn't recognize that, the 503 propagates as an unhandled rejection,
    caught only by syncOpenScore's outer try/catch, which logs an error and
    never toasts."""
    r = _flow("""
__setOnline(false);
__state.annotations["0"] = [{ uuid: "s1", type: "ink" }];
await M.saveAnnotations();
await __settle();
console.log(JSON.stringify({
  toasts: __log.filter((l) => l.startsWith("viewer:showToast")),
  errors: __errors,
  offlineFlag: __state.annotationsOffline,
  pending: [...__idb.keys()],
}));
""", _base_state(annotationEtag=None, annotationBase=None))
    assert r["errors"] == [], f"an offline stroke must not log an error: {r['errors']}"
    assert len(r["toasts"]) == 1
    assert r["offlineFlag"] is True
    assert r["pending"] == ["/m/A.pdf"]


# ---------------------------------------------------------------------------
# Review fix (b): a background sync for a score that gets opened while it's
# still queued must update the open score's etag/base, or its next save
# conflicts against its own now-stale etag.
# ---------------------------------------------------------------------------

def test_opening_a_score_mid_background_sync_does_not_cause_a_false_conflict():
    r = _flow("""
__setServer("/m/A.pdf", { pages: {}, rotations: {}, etag: "e0" });
await O.putPending({
  path: "/m/A.pdf", base: { pages: {}, rotations: {} }, baseEtag: "e0",
  pages: { "0": [{ uuid: "b1", type: "ink" }] }, rotations: {},
});

// Launch-time sync starts (fire-and-forget, as app.js does): score A isn't
// open yet, so its entry is synced on the queued save chain (not through
// saveAnnotations()). Let allPending() resolve and the loop dispatch the
// job onto _saveChain (microtasks only) before opening the score, but
// before its gated network call (real latency, above) has resolved — a
// setTimeout(0) macrotask lands squarely in that window.
__state.currentScore = null;
M.syncPendingAnnotations();
await new Promise((r) => setTimeout(r, 0));

// Before that job's PUT resolves, the user opens score A: the load path
// already applied the outbox copy (annotationState would have returned it),
// still carrying the pre-sync etag.
__state.currentScore = { filepath: "/m/A.pdf" };
__state.annotationEtag = "e0";
__state.annotationBase = { pages: {}, rotations: {} };
__state.annotations = { "0": [{ uuid: "b1", type: "ink" }] };
__state.rotations = {};

await __settle();     // let the queued background sync finish
const etagAfterBackgroundSync = __state.annotationEtag;

// A normal save now happens on the (still open) score.
__state.annotations["0"].push({ uuid: "b2", type: "ink" });
await M.saveAnnotations();
await __settle();

console.log(JSON.stringify({
  etagAfterBackgroundSync,
  finalEtag: __state.annotationEtag,
  dialogs: __log.filter((l) => l.startsWith("dialog-handlers:showConflictDialog")),
  putEtags: __calls.filter((c) => c.method === "PUT").map((c) => c.body.expected_etag),
}));
""", _base_state())
    assert r["etagAfterBackgroundSync"] not in ("e0", None), (
        "the open score's etag must be brought up to date by the background "
        "sync (applySynced), or its own next save will conflict against a "
        "stale etag"
    )
    assert r["dialogs"] == [], "no real conflict occurred — this must not show the conflict dialog"
    assert r["putEtags"] == ["e0", r["etagAfterBackgroundSync"]]


# ---------------------------------------------------------------------------
# Review fix (c): a null (unknown) etag must never be sent as, or treated
# as, "no check" (an unconditional overwrite).
# ---------------------------------------------------------------------------

def test_annotation_state_keeps_a_real_empty_etag_distinct_from_unknown():
    """"" is the server's real etag for "no sidecar yet"; null means the
    base is unknown (loaded offline with nothing cached). `?? null`
    preserves that distinction; `|| null` would collapse them, since "" is
    falsy."""
    r = _flow("""
console.log(JSON.stringify({
  freshScore: M.annotationState({ pages: {}, rotations: {}, etag: "" }, undefined),
  unknownBase: M.annotationState({ pages: {}, rotations: {}, etag: null }, undefined),
}));
""", _base_state())
    assert r["freshScore"]["etag"] == "", "a brand-new score's real etag (\"\") must not become null"
    assert r["unknownBase"]["etag"] is None


def test_brand_new_score_saves_through_the_normal_put_not_the_outbox():
    r = _flow("""
__setServer("/m/A.pdf", { pages: {}, rotations: {}, etag: "" });
__state.annotations["0"] = [{ uuid: "s1", type: "ink" }];
await M.saveAnnotations();
await __settle();
console.log(JSON.stringify({
  calls: __calls.map((c) => ({ method: c.method, expected_etag: c.body && c.body.expected_etag })),
  pending: [...__idb.keys()],
}));
""", _base_state(annotationEtag=""))
    assert r["calls"] == [{"method": "PUT", "expected_etag": ""}]
    assert r["pending"] == []


def test_online_save_with_unknown_etag_merges_with_the_server_instead_of_overwriting_it():
    """Regression scenario for (c): if the null-etag routing to the outbox
    were dropped, this save would go straight to a normal PUT with no
    expected_etag at all (the normal path only sets it when annotationEtag
    is not null) — an unconditional overwrite that would silently discard
    the other device's annotation on page "5"."""
    r = _flow("""
__setServer("/m/A.pdf", { pages: { "5": [{ uuid: "other-device", type: "ink" }] }, rotations: {}, etag: "e-real" });
__state.annotations["0"] = [{ uuid: "local1", type: "ink" }];
await M.saveAnnotations();
await __settle();
const allUuids = Object.values(__state.annotations).flat().map((a) => a.uuid);
console.log(JSON.stringify({
  calls: __calls.map((c) => c.method),
  allUuids,
  finalEtag: __state.annotationEtag,
}));
""", _base_state(annotationEtag=None, annotationBase=None))
    assert r["calls"] == ["GET", "PUT"], "an unknown base must merge with the server before ever PUTting"
    assert set(r["allUuids"]) == {"local1", "other-device"}, "the other device's annotation must not be lost"
    assert r["finalEtag"] not in (None, "")


# ---------------------------------------------------------------------------
# 5. Ordinary online behaviour is unaffected: a real conflict still shows
# the dialog, and Reload drops any outbox entry.
# ---------------------------------------------------------------------------

def test_an_ordinary_online_409_with_no_outbox_entry_still_shows_the_conflict_dialog():
    r = _flow("""
__setServer("/m/A.pdf", { pages: {}, rotations: {}, etag: "e-server-side" });
__state.annotations["0"] = [{ uuid: "s1", type: "ink" }];
await M.saveAnnotations();
await __settle();
console.log(JSON.stringify({
  dialogs: __log.filter((l) => l.startsWith("dialog-handlers:showConflictDialog")),
  pending: [...__idb.keys()],
  finalEtag: __state.annotationEtag,
}));
""", _base_state(annotationEtag="e-stale-client-side"))
    assert len(r["dialogs"]) == 1
    assert r["pending"] == [], "an ordinary online conflict must not go through the outbox"
    assert r["finalEtag"] == "e-stale-client-side"    # unchanged: the save was rejected, not applied


def test_reload_drops_any_outbox_entry_for_the_score():
    r = _flow("""
await O.putPending({
  path: "/m/A.pdf", base: { pages: {}, rotations: {} }, baseEtag: "e0",
  pages: { "0": [{ uuid: "b1", type: "ink" }] }, rotations: {},
});
await M.annotationsReloaded(__state, { pages: { "0": [{ uuid: "server1", type: "ink" }] }, rotations: {}, etag: "e-new" });
console.log(JSON.stringify({
  pending: [...__idb.keys()],
  uuids: (__state.annotations["0"] || []).map((a) => a.uuid),
  offlineFlag: __state.annotationsOffline,
  etag: __state.annotationEtag,
}));
""", _base_state(annotationsOffline=True))
    assert r["pending"] == []
    assert r["uuids"] == ["server1"]
    assert r["offlineFlag"] is False
    assert r["etag"] == "e-new"


# ---------------------------------------------------------------------------
# 6. syncPendingAnnotations: non-open scores sync in order on the save
# chain; the open score's goes through saveAnnotations().
# ---------------------------------------------------------------------------

def test_pending_scores_sync_in_order_and_the_open_one_goes_through_save_annotations():
    r = _flow("""
for (const p of ["/m/A.pdf", "/m/B.pdf", "/m/C.pdf"]) {
  __setServer(p, { pages: {}, rotations: {}, etag: "e0" });
  await O.putPending({
    path: p, base: { pages: {}, rotations: {} }, baseEtag: "e0",
    pages: { "0": [{ uuid: p, type: "ink" }] }, rotations: {},
  });
}
__state.currentScore = { filepath: "/m/A.pdf" };   // A is the open score
__state.annotationEtag = "e0";
__state.annotations = { "0": [{ uuid: "/m/A.pdf", type: "ink" }] };
M.syncPendingAnnotations();
await __settle();
await __settle();
console.log(JSON.stringify({
  order: __calls.filter((c) => c.method === "PUT").map((c) => c.body.path),
  pending: [...__idb.keys()],
}));
""", _base_state(currentScore=None, annotationEtag=None))
    assert r["order"] == ["/m/A.pdf", "/m/B.pdf", "/m/C.pdf"]
    assert r["pending"] == []
