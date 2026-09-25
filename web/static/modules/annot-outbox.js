// ---------------------------------------------------------------------------
// Annotations saved while offline
//
// A save that can't reach the server is kept here, per score, until it
// does: the score's whole annotation state, plus the server state it was
// based on (`base`, `baseEtag`). Opening the score shows it, so offline
// strokes survive closing the score or the app. Syncing merges it with
// whatever the server has by then (e.g. edits made on another device), so
// neither side's annotations are lost. DOM-free.
// ---------------------------------------------------------------------------

import { api } from "./api.js";

const DB_NAME = "folio-outbox";

// One connection, opened on first use. It closes itself if a newer version
// of the app needs to upgrade the database, and is reopened on next use.
let _db = null;

function openDb() {
  _db ||= new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore("pending", { keyPath: "path" });
    };
    req.onsuccess = () => {
      const db = req.result;
      db.onversionchange = () => { db.close(); _db = null; };
      resolve(db);
    };
    req.onerror = () => { _db = null; reject(req.error); };
  });
  return _db;
}

async function run(mode, fn) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("pending", mode);
    const req = fn(tx.objectStore("pending"));
    tx.oncomplete = () => resolve(req.result);
    tx.onerror = () => reject(tx.error);
  });
}

// { path, base: {pages, rotations}, baseEtag, pages, rotations } or undefined
export const getPending = (path) => run("readonly", (st) => st.get(path));
export const allPending = () => run("readonly", (st) => st.getAll());
export const putPending = (entry) => run("readwrite", (st) => st.put(entry));
export const deletePending = (path) => run("readwrite", (st) => st.delete(path));

// ---------------------------------------------------------------------------
// Three-way merge
// ---------------------------------------------------------------------------

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// uuid -> { pg, a } across all pages, in page and list order.
function byUuid(pages) {
  const m = new Map();
  for (const [pg, list] of Object.entries(pages || {})) {
    for (const a of list || []) m.set(a.uuid, { pg, a });
  }
  return m;
}

// Merge `local` and `remote` annotation states ({pages, rotations}) that
// both started from `base`. Every annotation added on either side is kept;
// one changed on one side takes that side's version (local's, if both
// changed it); one deleted on either side stays deleted. Remote's order is
// kept, with local additions after it. The start-page stamp is one per
// score: if local placed, moved or removed it, local's wins.
export function mergeAnnotations(base, local, remote) {
  const B = byUuid(base.pages), L = byUuid(local.pages), R = byUuid(remote.pages);
  const changedLocally = (id) => !B.has(id) || !same(L.get(id), B.get(id));
  const merged = [];                       // [{ pg, a }] in drawing order
  for (const [id, r] of R) {
    if (B.has(id) && !L.has(id)) continue;           // deleted locally
    merged.push(L.has(id) && changedLocally(id) ? L.get(id) : r);
  }
  for (const [id, l] of L) {
    if (!R.has(id) && !B.has(id)) merged.push(l);    // added locally
    // (in base but not remote: deleted remotely, stays deleted)
  }

  const starts = (m) => [...m.values()].filter((e) => e.a.type === "startpage");
  const localStartChanged = !same(starts(L), starts(B));
  const keep = merged.filter((e) => e.a.type !== "startpage"
    || (localStartChanged ? L : R).has(e.a.uuid));

  const pages = {};
  for (const { pg, a } of keep) (pages[pg] ||= []).push(a);

  const rot = (s, k) => (s.rotations || {})[k] || 0;
  const rotations = {};
  const keys = new Set([base, local, remote].flatMap((s) => Object.keys(s.rotations || {})));
  for (const k of keys) {
    const v = rot(local, k) !== rot(base, k) ? rot(local, k) : rot(remote, k);
    if (v) rotations[k] = v;
  }
  return { pages, rotations };
}

// ---------------------------------------------------------------------------
// Sync
// ---------------------------------------------------------------------------

// No network, as opposed to an error from the server: a failed fetch(), or
// the service worker's 503 for an API GET it has no cached copy of.
export const isOffline = (err) =>
  err instanceof TypeError || String(err?.message).startsWith("503");
export const annotUrl = (path) => `/api/annotations?path=${encodeURIComponent(path)}`;

// `entry` merged with the server's current annotations, based on them: or
// null if offline.
async function mergedWithServer(entry) {
  let remote;
  try {
    remote = await api(annotUrl(entry.path));
  } catch (err) {
    if (isOffline(err)) return null;
    throw err;
  }
  const theirs = { pages: remote.pages || {}, rotations: remote.rotations || {} };
  const merged = { ...entry,
    ...mergeAnnotations(entry.base || { pages: {}, rotations: {} }, entry, theirs),
    base: theirs, baseEtag: remote.etag ?? "" };
  await putPending(merged);
  return merged;
}

// Save a pending `entry` to the server, merging with the server's state if
// it changed since `entry.base` (or if the base is unknown: a score first
// opened offline). Returns the state the server now holds, {etag, pages,
// rotations}, and removes the entry; or null if still offline, keeping it.
// Throws on other errors (e.g. the score is gone), keeping it.
export async function syncEntry(entry) {
  let e = entry;
  if (e.baseEtag == null && !(e = await mergedWithServer(e))) return null;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const result = await api("/api/annotations", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: e.path, pages: e.pages, rotations: e.rotations, expected_etag: e.baseEtag,
        }),
      });
      await deletePending(e.path);
      return { etag: result.etag, pages: e.pages, rotations: e.rotations };
    } catch (err) {
      if (isOffline(err)) return null;
      if (!String(err.message).startsWith("409")) throw err;
      if (!(e = await mergedWithServer(e))) return null;
    }
  }
  throw new Error(`annotations for ${e.path} kept changing while syncing; will retry`);
}
