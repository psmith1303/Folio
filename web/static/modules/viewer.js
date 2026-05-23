// ---------------------------------------------------------------------------
// PDF viewer — rendering, page navigation, display modes, fullscreen
// ---------------------------------------------------------------------------

// pdf.js is self-hosted under /lib/pdfjs/ (vendored copy of pdfjs-dist@5.4.149).
// Same-origin loading means real error messages — cross-origin scripts get
// their errors masked as "Script error." with no detail, which made debugging
// pdf.js failures over Tailscale impossible.
const PDFJS_BASE = "/lib/pdfjs";

import * as pdfjsLib from "/lib/pdfjs/build/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_BASE + "/build/pdf.worker.min.mjs";

import { getState, resetViewerState, resetAnnotationState } from "./state.js";
import {
  pdfContainer, canvas1, canvas2, annotCanvas1, annotCanvas2,
  pageWrap2, pageInput, pageTotal, titleDisplay,
  btnClose, btnZoomFit, btnZoomWide, btnSideBySide,
  btnPrev, btnNext, btnExport, btnFullscreen,
  libraryStatus, viewerToast,
} from "./dom.js";
import { api } from "./api.js";
import { showView } from "./views.js";
import {
  drawAnnotations, setTool, setNavCallbacks, setRenderPageFn,
  setInvalidatePrerenderFn,
} from "./annotations.js";
import { addToRecent } from "./recent.js";
import { CACHE_AVAILABLE, refreshCacheStatus } from "./cache.js";

// Register callbacks so annotations module can trigger navigation
setNavCallbacks(nextPage, prevPage);
setRenderPageFn(renderPage);
setInvalidatePrerenderFn(invalidatePrerender);

// Verbose viewer logging — enable in DevTools with: localStorage.folioDebug = "1"
const VIEWER_TAG = "[viewer v2.8.10]";
function dbg(...args) {
  if (typeof localStorage !== "undefined" && localStorage.folioDebug === "1") {
    console.log(VIEWER_TAG, ...args);
  }
}
console.log(VIEWER_TAG, "module loaded — set localStorage.folioDebug='1' for verbose logs");

// ---------------------------------------------------------------------------
// Toast helper — transient status message inside the viewer
// ---------------------------------------------------------------------------

let _toastTimer = null;

export function showToast(msg, { duration = 4000 } = {}) {
  if (!viewerToast) return;
  viewerToast.textContent = msg;
  viewerToast.classList.remove("hidden");
  if (_toastTimer) clearTimeout(_toastTimer);
  if (duration > 0) {
    _toastTimer = setTimeout(() => hideToast(), duration);
  }
}

export function hideToast() {
  if (!viewerToast) return;
  viewerToast.classList.add("hidden");
  if (_toastTimer) {
    clearTimeout(_toastTimer);
    _toastTimer = null;
  }
}

// ---------------------------------------------------------------------------
// Shared PDF loading logic (used by openScore and openSetlistSong)
//
// Loads annotations + PDF first, only mutating viewer state after both
// succeed. A failure mid-load therefore leaves the current viewer intact
// so callers can recover (e.g. stay on the previous song during setlist
// auto-advance) instead of being bounced back to the library.
// ---------------------------------------------------------------------------

// Single-shot GET with retry + cache self-heal. Returns the PdfDocumentProxy.
//
// disableRange + disableStream: fetch the PDF as a single full GET. HTTPS
// proxies (Tailscale Serve, cloudflared, etc.) can truncate chunked-stream
// range responses mid-flight, which surfaces as "Bad end offset" errors
// from pdf.js. A single-shot GET paired with the service worker's cache
// avoids the proxy-streaming failure mode at the cost of a slower first
// open for large PDFs — which the SW cache then makes instant next time.
//
// Self-heal: before retrying, purge the PDF_CACHE entry for this path.
// A prior truncated write can poison the cache; dropping it forces the
// next attempt to go back to the network instead of replaying corruption.
async function _fetchPdfDoc(filepath, { showRetryToast = true } = {}) {
  let lastErr = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const loadingTask = pdfjsLib.getDocument({
        url: `/api/pdf?path=${encodeURIComponent(filepath)}&_t=${Date.now()}`,
        wasmUrl: PDFJS_BASE + "/wasm/",
        disableRange: true,
        disableStream: true,
      });
      return await loadingTask.promise;
    } catch (err) {
      lastErr = err;
      console.warn(VIEWER_TAG, `PDF load attempt ${attempt + 1} failed:`, err);
      if (attempt + 1 < 2) {
        try {
          const cache = await caches.open("folio-pdfs-v1");
          const purged = await cache.delete(
            "/api/pdf?path=" + encodeURIComponent(filepath)
          );
          if (purged) console.warn(VIEWER_TAG, "purged corrupt cache entry for", filepath);
        } catch (e) {
          console.warn(VIEWER_TAG, "cache purge failed:", e);
        }
        if (showRetryToast) showToast(`Load failed — retrying…`, { duration: 0 });
        await new Promise((r) => setTimeout(r, 800));
      }
    }
  }
  throw lastErr;
}

async function _fetchAnnotations(filepath) {
  try {
    return await api(`/api/annotations?path=${encodeURIComponent(filepath)}`);
  } catch {
    return { pages: {}, rotations: {}, etag: null };
  }
}

async function loadAndRenderPdf(filepath, { startPage = 1, prefetched = null } = {}) {
  const s = getState();

  let annotData, newDoc;
  if (prefetched && prefetched.path === filepath && prefetched.pdfDoc) {
    dbg("loadAndRenderPdf: using prefetched bundle for", filepath);
    annotData = prefetched.annotData || { pages: {}, rotations: {}, etag: null };
    newDoc = prefetched.pdfDoc;
  } else {
    annotData = await _fetchAnnotations(filepath);
    newDoc = await _fetchPdfDoc(filepath);
  }

  // Commit state only after successful load. Destroy the previous doc to
  // release its worker resources — without this, prefetch + setlist
  // playback can pile up live PDF instances.
  if (s.pdfDoc && s.pdfDoc !== newDoc) {
    try { s.pdfDoc.destroy(); } catch { /* ignore */ }
  }
  resetAnnotationState();
  s.pdfDoc = newDoc;
  s.annotations = annotData.pages || {};
  s.rotations = annotData.rotations || {};
  s.annotationEtag = annotData.etag || null;
  s.totalPages = s.pdfDoc.numPages;
  pageTotal.textContent = s.totalPages;
  pageInput.max = s.totalPages;

  // Clamp startPage — pass Number.MAX_SAFE_INTEGER to land on the last page.
  s.currentPage = Math.max(1, Math.min(startPage, s.totalPages));
  pageInput.value = s.currentPage;

  hideToast();
  await autoSideBySide();
  await renderPage();
  pdfContainer.focus();
}

// ---------------------------------------------------------------------------
// Open / close
// ---------------------------------------------------------------------------

// Callback for library reload on missing file — set by library module
let _loadLibrary = null;
export function setLoadLibraryFn(fn) { _loadLibrary = fn; }

export async function openScore(score, { startPage = 1 } = {}) {
  const s = getState();
  dbg("openScore", score.filepath, "startPage", startPage);
  s.currentScore = score;
  s.setlistPlayback = null;
  s.returnView = s.currentView;
  titleDisplay.textContent = `${score.composer} — ${score.title}`;
  showView("viewer");
  // Indefinite toast — cleared by loadAndRenderPdf's hideToast() on success,
  // or overwritten by the catch branch on failure.
  showToast(`Loading "${score.title}"…`, { duration: 0 });

  try {
    await loadAndRenderPdf(score.filepath, { startPage });
    addToRecent(score);
  } catch (err) {
    console.warn(VIEWER_TAG, "openScore failed → bouncing to library:", err);
    if (err.message && err.message.includes("404")) {
      try { await api("/api/library/rescan", { method: "POST" }); } catch { /* ignore */ }
      if (_loadLibrary) await _loadLibrary();
      showView("library");
      libraryStatus.textContent = `"${score.title}" is no longer available — library refreshed`;
    } else {
      cleanupScore();
      showView("library");
      libraryStatus.textContent = `Failed to load "${score.title}": ${err.message}`;
    }
  }
}

export function cleanupScore() {
  cleanupAllPages();
  dropPrefetchedSong();
  resetViewerState();
  setTool("nav");
  canvas1.width = 0;  canvas1.height = 0;
  canvas2.width = 0;  canvas2.height = 0;
  annotCanvas1.width = 0;  annotCanvas1.height = 0;
  annotCanvas2.width = 0;  annotCanvas2.height = 0;
  pageWrap2.classList.add("hidden");
  titleDisplay.textContent = "";
}

export function closeScore() {
  const returnTo = getState().returnView;
  cleanupScore();
  showView(returnTo);
  // The SW auto-caches PDFs on first open; refresh button states so the
  // library reflects the new cache entry without needing a second visit.
  if (CACHE_AVAILABLE) refreshCacheStatus().catch(() => {});
}

// ---------------------------------------------------------------------------
// Setlist song prefetch
//
// While the user is reading the current song, fetch the next song's
// annotations + parse its PDF in the background. On the song boundary
// (last-page → next, or auto-advance), openSetlistSong consumes the
// prefetched bundle and skips network + parse entirely.
// ---------------------------------------------------------------------------

async function dropPrefetchedSong() {
  const s = getState();
  if (!s.prefetchedSong) return;
  const slot = s.prefetchedSong;
  s.prefetchedSong = null;
  if (slot.pdfDoc) {
    try { slot.pdfDoc.destroy(); } catch { /* ignore */ }
  }
}

async function prefetchNextSetlistSong() {
  const s = getState();
  if (!s.setlistPlayback) return;
  const nextIdx = s.setlistPlayback.index + 1;
  const song = s.setlistPlayback.songs[nextIdx];
  if (!song) return;

  // Already prefetched (or in flight) for this song — nothing to do.
  if (s.prefetchedSong && s.prefetchedSong.path === song.path) return;

  // Different song was queued (e.g. setlist index jumped) — discard it.
  if (s.prefetchedSong) await dropPrefetchedSong();

  const slot = { index: nextIdx, path: song.path, pdfDoc: null, annotData: null };
  s.prefetchedSong = slot;
  dbg("prefetchNextSetlistSong: starting prefetch for", song.path);

  try {
    const [annotData, pdfDoc] = await Promise.all([
      _fetchAnnotations(song.path),
      _fetchPdfDoc(song.path, { showRetryToast: false }),
    ]);
    // Slot may have been claimed (openSetlistSong) or replaced (drop +
    // re-prefetch) while we were awaiting. If so, the doc isn't ours
    // to keep — destroy it to free the worker.
    if (s.prefetchedSong !== slot) {
      try { pdfDoc.destroy(); } catch { /* ignore */ }
      return;
    }
    slot.annotData = annotData;
    slot.pdfDoc = pdfDoc;
    dbg("prefetchNextSetlistSong: ready for", song.path);
  } catch (err) {
    dbg("prefetchNextSetlistSong: failed for", song.path, err);
    if (s.prefetchedSong === slot) s.prefetchedSong = null;
  }
}

// ---------------------------------------------------------------------------
// Setlist song opening
// ---------------------------------------------------------------------------

export async function openSetlistSong(index, goToEnd = false, { autoAdvance = false } = {}) {
  const s = getState();
  const song = s.setlistPlayback.songs[index];
  const total = s.setlistPlayback.songs.length;
  dbg("openSetlistSong", { index, goToEnd, autoAdvance, path: song.path });

  const targetPage = goToEnd
    ? (song.end_page || Number.MAX_SAFE_INTEGER)
    : Math.max(1, song.start_page || 1);

  const prevTitle = titleDisplay.textContent;
  titleDisplay.textContent = `Loading ${song.composer} — ${song.title}…`;

  // Claim the prefetched bundle if it matches this song. Claiming clears
  // the state slot so the in-progress prefetch promise (if any) doesn't
  // race with a fresh load, and so dropPrefetchedSong() won't destroy
  // a doc we're about to mount.
  let prefetched = null;
  if (s.prefetchedSong && s.prefetchedSong.path === song.path && s.prefetchedSong.pdfDoc) {
    prefetched = s.prefetchedSong;
    s.prefetchedSong = null;
    dbg("openSetlistSong: claimed prefetch for", song.path);
  } else {
    // Different song than the one we prefetched — drop it before loading.
    await dropPrefetchedSong();
  }

  try {
    await loadAndRenderPdf(song.path, { startPage: targetPage, prefetched });
    // Commit setlist position and title only after load succeeds
    s.setlistPlayback.index = index;
    s.currentScore = { filepath: song.path, composer: song.composer, title: song.title };
    titleDisplay.textContent = `${song.composer} — ${song.title} (${index + 1}/${total})`;
    addToRecent({ filepath: song.path, composer: song.composer, title: song.title });
  } catch (err) {
    // Auto-advance at a song boundary: keep the user on the current song
    // and surface the failure as a toast — don't bounce to the library.
    if (autoAdvance) {
      titleDisplay.textContent = prevTitle;
      console.warn(VIEWER_TAG, "auto-advance failed (staying in viewer):", err);
      showToast(`Couldn't load "${song.title}" — press ${goToEnd ? "←" : "→"} to retry`);
      return;
    }
    console.warn(VIEWER_TAG, "openSetlistSong failed → bouncing to library:", err);
    if (err.message && err.message.includes("404")) {
      try { await api("/api/library/rescan", { method: "POST" }); } catch { /* ignore */ }
      if (_loadLibrary) await _loadLibrary();
      showView("library");
      libraryStatus.textContent = `"${song.title}" is no longer available — library refreshed`;
    } else {
      cleanupScore();
      showView("library");
      libraryStatus.textContent = `Failed to load "${song.title}": ${err.message}`;
    }
  }
}

// ---------------------------------------------------------------------------
// Page rendering
// ---------------------------------------------------------------------------

export async function renderPage() {
  const s = getState();
  if (!s.pdfDoc || s.rendering) return;
  s.rendering = true;

  pageInput.value = s.currentPage;
  dbg("renderPage", { page: s.currentPage, mode: s.displayMode });

  try {
    s.pageLayouts = [];

    const layout1 = await renderSinglePage(s.currentPage, canvas1, annotCanvas1);
    s.pageLayouts.push({ page: s.currentPage, ...layout1 });

    if (s.displayMode === "2up" && s.currentPage + 1 <= s.totalPages) {
      pageWrap2.classList.remove("hidden");
      const layout2 = await renderSinglePage(s.currentPage + 1, canvas2, annotCanvas2);
      s.pageLayouts.push({ page: s.currentPage + 1, ...layout2 });
    } else {
      pageWrap2.classList.add("hidden");
      canvas2.width = 0;  canvas2.height = 0;
      annotCanvas2.width = 0;  annotCanvas2.height = 0;
    }

    drawAnnotations();
    cleanupOldPages();

    if (s.scrollToBottomAfterRender) {
      pdfContainer.scrollTop = pdfContainer.scrollHeight;
      s.scrollToBottomAfterRender = false;
    } else {
      pdfContainer.scrollTop = 0;
    }
    hideToast();
  } catch (err) {
    console.error(VIEWER_TAG, "renderPage failed:", err);
    cleanupAllPages();
    const detail = err && err.message ? `: ${err.message}` : "";
    showToast(`Page ${s.currentPage} failed to render${detail} — press ←/→ to retry`);
  } finally {
    s.rendering = false;
  }

  // Fire-and-forget neighbor prerender + next-song prefetch. Both stay off
  // the critical path of the visible render — they only matter for the
  // *next* navigation, not the one we just completed.
  prerenderNeighbors();
  prefetchNextSetlistSong();
}

// Rasterizes a page to a detached canvas. Returns a cache entry suitable
// for blitting. Held separately from cachedPages because the offscreen
// canvas is the expensive part — getPage() is cheap compared to render().
async function _rasterizePageImpl(pageNum) {
  const s = getState();
  let page = s.cachedPages.get(pageNum);
  if (!page) {
    page = await s.pdfDoc.getPage(pageNum);
    s.cachedPages.set(pageNum, page);
  }
  // PDF.js's getViewport({rotation}) OVERRIDES the page's intrinsic /Rotate
  // rather than adding to it. Combine them so PDFs that declare a non-zero
  // intrinsic rotation render in their canonical (Acrobat) orientation, with
  // the user's stored rotation applied on top.
  const userRot = (s.rotations[String(pageNum - 1)] || 0) % 360;
  const totalRot = (((page.rotate || 0) + userRot) % 360 + 360) % 360;

  const layoutCtx = _layoutContext();
  const containerHeight = layoutCtx.containerH - 16;
  const containerWidth = layoutCtx.displayMode === "2up"
    ? (layoutCtx.containerW - 20) / 2
    : layoutCtx.containerW - 16;

  const unscaledViewport = page.getViewport({ scale: 1, rotation: totalRot });
  const scaleW = containerWidth / unscaledViewport.width;
  const scaleH = containerHeight / unscaledViewport.height;
  const scale = layoutCtx.displayMode === "wide" ? scaleW : Math.min(scaleW, scaleH);
  const viewport = page.getViewport({ scale, rotation: totalRot });
  const dpr = layoutCtx.dpr;

  const cssW = Math.floor(viewport.width);
  const cssH = Math.floor(viewport.height);

  const off = document.createElement("canvas");
  off.width = Math.floor(viewport.width * dpr);
  off.height = Math.floor(viewport.height * dpr);
  const ctx = off.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  await page.render({ canvasContext: ctx, viewport }).promise;

  return {
    canvas: off,
    cssW, cssH, dpr,
    userRot,
    displayMode: layoutCtx.displayMode,
    containerW: layoutCtx.containerW,
    containerH: layoutCtx.containerH,
  };
}

const _rasterInFlight = new Map();

// Cache-and-dedupe wrapper. Two concurrent callers asking for the same page
// share the same in-flight rasterization promise instead of duplicating work.
async function _getPrerenderEntry(pageNum) {
  const s = getState();
  const cached = s.prerenderedPages.get(pageNum);
  if (cached && _prerenderValid(cached, pageNum)) return cached;
  if (cached) s.prerenderedPages.delete(pageNum);

  if (_rasterInFlight.has(pageNum)) {
    return _rasterInFlight.get(pageNum);
  }
  const promise = _rasterizePageImpl(pageNum);
  _rasterInFlight.set(pageNum, promise);
  try {
    const entry = await promise;
    if (_prerenderValid(entry, pageNum)) {
      s.prerenderedPages.set(pageNum, entry);
    }
    return entry;
  } finally {
    _rasterInFlight.delete(pageNum);
  }
}

function _layoutContext() {
  return {
    displayMode: getState().displayMode,
    containerW: pdfContainer.clientWidth,
    containerH: pdfContainer.clientHeight,
    dpr: window.devicePixelRatio || 1,
  };
}

function _prerenderValid(entry, pageNum) {
  if (!entry) return false;
  const s = getState();
  const ctx = _layoutContext();
  const userRot = (s.rotations[String(pageNum - 1)] || 0) % 360;
  return entry.displayMode === ctx.displayMode
    && entry.containerW === ctx.containerW
    && entry.containerH === ctx.containerH
    && entry.dpr === ctx.dpr
    && entry.userRot === userRot;
}

export function invalidatePrerenders() {
  getState().prerenderedPages.clear();
}

export function invalidatePrerender(pageNum) {
  getState().prerenderedPages.delete(pageNum);
}

async function renderSinglePage(pageNum, pdfCanvas, annotCanvas) {
  // Guard against blitting an entry whose layout no longer matches the
  // viewer (in-flight raster started before a display-mode change or
  // resize). If stale, drop it and rasterize fresh at the current layout.
  let entry = await _getPrerenderEntry(pageNum);
  if (!_prerenderValid(entry, pageNum)) {
    getState().prerenderedPages.delete(pageNum);
    entry = await _getPrerenderEntry(pageNum);
  }
  const { canvas: off, cssW, cssH, dpr } = entry;

  pdfCanvas.width = off.width;
  pdfCanvas.height = off.height;
  pdfCanvas.style.width = cssW + "px";
  pdfCanvas.style.height = cssH + "px";
  const ctx = pdfCanvas.getContext("2d");
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.drawImage(off, 0, 0);

  annotCanvas.width = Math.floor(cssW * dpr);
  annotCanvas.height = Math.floor(cssH * dpr);
  annotCanvas.style.width = cssW + "px";
  annotCanvas.style.height = cssH + "px";

  return { cssW, cssH };
}

// Background prerender of the next view (and the previous view, since users
// often page backwards by one). Pages are stored as detached canvases that
// the next renderPage can blit instantly instead of re-rasterizing.
async function prerenderNeighbors() {
  const s = getState();
  if (!s.pdfDoc) return;
  const step = s.displayMode === "2up" ? 2 : 1;

  const targets = [];
  for (let i = 0; i < step; i++) {
    const p = s.currentPage + step + i;
    if (p <= s.totalPages) targets.push(p);
  }
  for (let i = 0; i < step; i++) {
    const p = s.currentPage - step + i;
    if (p >= 1) targets.push(p);
  }

  for (const pageNum of targets) {
    // Bail if a visible render started — we don't want background work
    // competing with the page the user is actively viewing.
    if (s.rendering) return;
    const existing = s.prerenderedPages.get(pageNum);
    if (existing && _prerenderValid(existing, pageNum)) continue;
    try {
      await _getPrerenderEntry(pageNum);
    } catch (err) {
      dbg("prerender failed for page", pageNum, err);
    }
  }
}

// ---------------------------------------------------------------------------
// Page cache management
// ---------------------------------------------------------------------------

function cleanupOldPages() {
  const s = getState();
  const step = s.displayMode === "2up" ? 2 : 1;
  const hot = new Set();
  for (const layout of s.pageLayouts) {
    hot.add(layout.page);
  }
  // Also keep prerendered neighbors hot — these match the set prerenderNeighbors
  // populates, so we don't churn pdf.js page objects between page turns.
  for (let i = 1; i <= step; i++) {
    hot.add(s.currentPage + step + i - 1);
    hot.add(s.currentPage - step + i - 1);
  }
  for (const [num, page] of s.cachedPages) {
    if (!hot.has(num)) {
      page.cleanup();
      s.cachedPages.delete(num);
    }
  }
  for (const num of s.prerenderedPages.keys()) {
    if (!hot.has(num)) s.prerenderedPages.delete(num);
  }
}

function cleanupAllPages() {
  const s = getState();
  for (const page of s.cachedPages.values()) {
    page.cleanup();
  }
  s.cachedPages.clear();
  s.prerenderedPages.clear();
}

// ---------------------------------------------------------------------------
// Page navigation
// ---------------------------------------------------------------------------

export function getPageRange() {
  const s = getState();
  if (!s.setlistPlayback) return { min: 1, max: s.totalPages };
  const song = s.setlistPlayback.songs[s.setlistPlayback.index];
  const min = Math.max(1, Math.min(song.start_page || 1, s.totalPages));
  const max = song.end_page ? Math.min(song.end_page, s.totalPages) : s.totalPages;
  return { min, max };
}

export function goToPage(n) {
  const s = getState();
  const range = getPageRange();
  const p = Math.max(range.min, Math.min(range.max, n));
  if (p !== s.currentPage) {
    s.currentPage = p;
    renderPage();
  }
}

export function nextPage() {
  const s = getState();
  const step = s.displayMode === "2up" ? 2 : 1;
  const range = getPageRange();
  if (s.currentPage + step > range.max) {
    if (s.setlistPlayback && s.setlistPlayback.index < s.setlistPlayback.songs.length - 1) {
      openSetlistSong(s.setlistPlayback.index + 1, false, { autoAdvance: true });
    }
    return;
  }
  goToPage(s.currentPage + step);
}

export function prevPage() {
  const s = getState();
  const step = s.displayMode === "2up" ? 2 : 1;
  const range = getPageRange();
  if (s.currentPage - step < range.min) {
    if (s.setlistPlayback && s.setlistPlayback.index > 0) {
      openSetlistSong(s.setlistPlayback.index - 1, true, { autoAdvance: true });
    }
    return;
  }
  goToPage(s.currentPage - step);
}

// ---------------------------------------------------------------------------
// Display modes
// ---------------------------------------------------------------------------

async function autoSideBySide() {
  const s = getState();
  if (s.userLockedMode) return;
  const before = s.displayMode;

  if (!s.pdfDoc || s.totalPages < 2 || s.currentPage >= s.totalPages) {
    s.displayMode = "fit";
  } else {
    const page = await s.pdfDoc.getPage(s.currentPage);
    s.cachedPages.set(s.currentPage, page);
    const userRot = (s.rotations[String(s.currentPage - 1)] || 0) % 360;
    const totalRot = (((page.rotate || 0) + userRot) % 360 + 360) % 360;
    const vp = page.getViewport({ scale: 1, rotation: totalRot });

    const containerH = pdfContainer.clientHeight - 16;
    const fitW = pdfContainer.clientWidth - 16;
    const dualW = (pdfContainer.clientWidth - 20) / 2;

    const fitScale = Math.min(fitW / vp.width, containerH / vp.height);
    const dualScale = Math.min(dualW / vp.width, containerH / vp.height);

    s.displayMode = dualScale >= fitScale ? "2up" : "fit";
  }
  if (s.displayMode !== before) invalidatePrerenders();
  updateModeButtons();
}

export async function checkAutoSideBySide() {
  const s = getState();
  if (!s.pdfDoc) return;
  const prev = s.displayMode;
  await autoSideBySide();
  return prev !== s.displayMode;
}

function updateModeButtons() {
  const s = getState();
  btnZoomFit.classList.toggle("active", s.displayMode === "fit");
  btnZoomWide.classList.toggle("active", s.displayMode === "wide");
  btnSideBySide.classList.toggle("active", s.displayMode === "2up");
}

// ---------------------------------------------------------------------------
// Fullscreen
// ---------------------------------------------------------------------------

export function applyFullscreen(fs) {
  const s = getState();
  s.pseudoFullscreen = fs;
  if (fs) setTool("nav");
  btnFullscreen.textContent = fs ? "Exit FS" : "Fullscreen";
  document.getElementById("topbar").classList.toggle("hidden", fs);
  document.getElementById("viewer-toolbar").classList.toggle("hidden", fs);
  document.getElementById("viewer").classList.toggle("pseudo-fullscreen", fs);
  if (s.pdfDoc) renderPage();
}

export function toggleFullscreen() {
  const s = getState();
  const entering = !s.pseudoFullscreen;
  applyFullscreen(entering);
  // Also try native fullscreen (hides browser chrome on desktop)
  if (entering && document.fullscreenEnabled) {
    document.documentElement.requestFullscreen().catch(() => {});
  } else if (!entering && document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  }
}

export function isFullscreen() {
  return getState().pseudoFullscreen || !!document.fullscreenElement;
}

// ---------------------------------------------------------------------------
// Export annotated PDF
// ---------------------------------------------------------------------------

async function handleExport() {
  const s = getState();
  if (!s.currentScore) return;
  try {
    btnExport.disabled = true;
    btnExport.textContent = "Exporting\u2026";
    const resp = await fetch(
      `/api/pdf/export?path=${encodeURIComponent(s.currentScore.filepath)}`
    );
    if (!resp.ok) throw new Error(`Export failed: ${resp.status}`);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `annotated_${s.currentScore.filepath.split("/").pop()}`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    console.error("Export failed:", err);
  } finally {
    btnExport.disabled = false;
    btnExport.textContent = "Export";
  }
}

// ---------------------------------------------------------------------------
// Init event listeners
// ---------------------------------------------------------------------------

export function initViewerEvents() {
  btnClose.addEventListener("click", closeScore);
  btnPrev.addEventListener("click", prevPage);
  btnNext.addEventListener("click", nextPage);

  pdfContainer.addEventListener("click", () => pdfContainer.focus());

  pageInput.addEventListener("change", () => {
    goToPage(parseInt(pageInput.value, 10) || 1);
    pdfContainer.focus();
  });

  btnZoomFit.addEventListener("click", () => {
    const s = getState();
    s.displayMode = "fit";
    s.userLockedMode = true;
    invalidatePrerenders();
    updateModeButtons();
    setTool("nav");
    renderPage();
  });

  btnZoomWide.addEventListener("click", () => {
    const s = getState();
    s.displayMode = "wide";
    s.userLockedMode = true;
    invalidatePrerenders();
    updateModeButtons();
    setTool("nav");
    renderPage();
  });

  btnSideBySide.addEventListener("click", () => {
    const s = getState();
    s.displayMode = "2up";
    s.userLockedMode = true;
    invalidatePrerenders();
    updateModeButtons();
    setTool("nav");
    renderPage();
  });

  btnExport.addEventListener("click", handleExport);
  btnFullscreen.addEventListener("click", toggleFullscreen);

  document.addEventListener("fullscreenchange", () => {
    // Only handle native fullscreen exit — don't let spurious events
    // (e.g. iPad PWA standalone mode) trigger fullscreen entry.
    if (!document.fullscreenElement && getState().pseudoFullscreen) {
      applyFullscreen(false);
    }
  });

  // Resize handler — debounced
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(async () => {
      const s = getState();
      if (s.pdfDoc) {
        invalidatePrerenders();
        await checkAutoSideBySide();
        renderPage();
      }
    }, 150);
  });
}
