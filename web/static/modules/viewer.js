// ---------------------------------------------------------------------------
// PDF viewer — rendering, page navigation, display modes, fullscreen
// ---------------------------------------------------------------------------

const PDFJS_CDN = "https://cdn.jsdelivr.net/npm/pdfjs-dist@5.4.149";

import * as pdfjsLib from "https://cdn.jsdelivr.net/npm/pdfjs-dist@5.4.149/build/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_CDN + "/build/pdf.worker.min.mjs";

import { getState, resetViewerState, resetAnnotationState } from "./state.js";
import {
  pdfContainer, canvas1, canvas2, annotCanvas1, annotCanvas2,
  pageWrap2, pageInput, pageTotal, titleDisplay,
  btnZoomFit, btnZoomWide, btnSideBySide,
  btnPrev, btnNext, btnExport, btnFullscreen,
  libraryStatus,
} from "./dom.js";
import { api } from "./api.js";
import { esc } from "./utils.js";
import { showView } from "./views.js";
import { drawAnnotations, setTool, setNavCallbacks, setRenderPageFn } from "./annotations.js";
import { addToRecent } from "./recent.js";

// Register callbacks so annotations module can trigger navigation
setNavCallbacks(nextPage, prevPage);
setRenderPageFn(renderPage);

// ---------------------------------------------------------------------------
// Shared PDF loading logic (used by openScore and openSetlistSong)
// ---------------------------------------------------------------------------

async function loadAndRenderPdf(filepath, { startPage = 1, goToEnd = false } = {}) {
  const s = getState();
  resetAnnotationState();

  // Load annotations
  try {
    const data = await api(`/api/annotations?path=${encodeURIComponent(filepath)}`);
    s.annotations = data.pages || {};
    s.rotations = data.rotations || {};
    s.annotationEtag = data.etag || null;
  } catch {
    // annotations remain at defaults from resetAnnotationState
  }

  const loadingTask = pdfjsLib.getDocument({
    url: `/api/pdf?path=${encodeURIComponent(filepath)}&_t=${Date.now()}`,
    wasmUrl: PDFJS_CDN + "/wasm/",
  });
  s.pdfDoc = await loadingTask.promise;
  s.totalPages = s.pdfDoc.numPages;
  pageTotal.textContent = s.totalPages;
  pageInput.max = s.totalPages;

  if (goToEnd) {
    const range = getPageRange();
    s.currentPage = range.max;
  } else {
    // Clamp startPage to actual page count (it may have been set before totalPages was known)
    s.currentPage = Math.max(1, Math.min(startPage, s.totalPages));
  }
  pageInput.value = s.currentPage;

  await autoSideBySide();
  renderPage();
  pdfContainer.focus();
}

// ---------------------------------------------------------------------------
// Open / close
// ---------------------------------------------------------------------------

// Callback for library reload on missing file — set by library module
let _loadLibrary = null;
export function setLoadLibraryFn(fn) { _loadLibrary = fn; }

export async function openScore(score) {
  const s = getState();
  s.currentScore = score;
  s.setlistPlayback = null;
  s.returnView = s.currentView;
  titleDisplay.textContent = `${score.composer} — ${score.title}`;
  showView("viewer");

  try {
    await loadAndRenderPdf(score.filepath);
    addToRecent(score);
  } catch (err) {
    if (err.message && err.message.includes("404")) {
      try { await api("/api/library/rescan", { method: "POST" }); } catch { /* ignore */ }
      if (_loadLibrary) await _loadLibrary();
      showView("library");
      libraryStatus.textContent = `"${score.title}" is no longer available — library refreshed`;
    } else {
      pdfContainer.innerHTML = `<p style="color:#f88;padding:20px">Failed to load PDF: ${esc(err.message)}</p>`;
    }
  }
}

export function cleanupScore() {
  cleanupAllPages();
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
}

// ---------------------------------------------------------------------------
// Setlist song opening
// ---------------------------------------------------------------------------

export async function openSetlistSong(index, goToEnd = false) {
  const s = getState();
  s.setlistPlayback.index = index;
  const song = s.setlistPlayback.songs[index];
  const total = s.setlistPlayback.songs.length;

  s.currentScore = { filepath: song.path, composer: song.composer, title: song.title };
  titleDisplay.textContent = `${song.composer} — ${song.title} (${index + 1}/${total})`;

  try {
    const range_min = Math.max(1, song.start_page || 1);
    await loadAndRenderPdf(song.path, {
      startPage: goToEnd ? undefined : range_min,
      goToEnd,
    });
    addToRecent({ filepath: song.path, composer: song.composer, title: song.title });
  } catch (err) {
    if (err.message && err.message.includes("404")) {
      try { await api("/api/library/rescan", { method: "POST" }); } catch { /* ignore */ }
      if (_loadLibrary) await _loadLibrary();
      showView("library");
      libraryStatus.textContent = `"${song.title}" is no longer available — library refreshed`;
    } else {
      pdfContainer.innerHTML = `<p style="color:#f88;padding:20px">Failed to load PDF: ${esc(err.message)}</p>`;
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
  s.pageLayouts = [];

  try {
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
    prefetchNextPage();

    if (s.scrollToBottomAfterRender) {
      pdfContainer.scrollTop = pdfContainer.scrollHeight;
      s.scrollToBottomAfterRender = false;
    } else {
      pdfContainer.scrollTop = 0;
    }
  } finally {
    s.rendering = false;
  }
}

async function renderSinglePage(pageNum, pdfCanvas, annotCanvas) {
  const s = getState();
  const page = await s.pdfDoc.getPage(pageNum);
  s.cachedPages.set(pageNum, page);
  const rot = (s.rotations[String(pageNum - 1)] || 0) % 360;

  const containerHeight = pdfContainer.clientHeight - 16;
  const containerWidth = s.displayMode === "2up"
    ? (pdfContainer.clientWidth - 20) / 2
    : pdfContainer.clientWidth - 16;

  const unscaledViewport = page.getViewport({ scale: 1, rotation: rot });
  const scaleW = containerWidth / unscaledViewport.width;
  const scaleH = containerHeight / unscaledViewport.height;
  const scale = s.displayMode === "wide" ? scaleW : Math.min(scaleW, scaleH);

  const viewport = page.getViewport({ scale, rotation: rot });
  const dpr = window.devicePixelRatio || 1;

  const cssW = Math.floor(viewport.width);
  const cssH = Math.floor(viewport.height);

  pdfCanvas.width = Math.floor(viewport.width * dpr);
  pdfCanvas.height = Math.floor(viewport.height * dpr);
  pdfCanvas.style.width = cssW + "px";
  pdfCanvas.style.height = cssH + "px";

  const ctx = pdfCanvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  await page.render({ canvasContext: ctx, viewport }).promise;

  annotCanvas.width = Math.floor(cssW * dpr);
  annotCanvas.height = Math.floor(cssH * dpr);
  annotCanvas.style.width = cssW + "px";
  annotCanvas.style.height = cssH + "px";

  return { cssW, cssH };
}

// ---------------------------------------------------------------------------
// Page cache management
// ---------------------------------------------------------------------------

function cleanupOldPages() {
  const s = getState();
  const hot = new Set();
  for (const layout of s.pageLayouts) {
    hot.add(layout.page);
    hot.add(layout.page + 1);
    if (layout.page > 1) hot.add(layout.page - 1);
  }
  for (const [num, page] of s.cachedPages) {
    if (!hot.has(num)) {
      page.cleanup();
      s.cachedPages.delete(num);
    }
  }
}

function cleanupAllPages() {
  const s = getState();
  for (const page of s.cachedPages.values()) {
    page.cleanup();
  }
  s.cachedPages.clear();
}

async function prefetchNextPage() {
  const s = getState();
  if (!s.pdfDoc) return;
  const step = s.displayMode === "2up" ? 2 : 1;
  const next = s.currentPage + step;
  if (next <= s.totalPages && !s.cachedPages.has(next)) {
    try {
      const page = await s.pdfDoc.getPage(next);
      s.cachedPages.set(next, page);
    } catch { /* ignore prefetch failures */ }
  }
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
      openSetlistSong(s.setlistPlayback.index + 1);
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
      openSetlistSong(s.setlistPlayback.index - 1, true);
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

  if (!s.pdfDoc || s.totalPages < 2 || s.currentPage >= s.totalPages) {
    s.displayMode = "fit";
  } else {
    const page = await s.pdfDoc.getPage(s.currentPage);
    s.cachedPages.set(s.currentPage, page);
    const rot = (s.rotations[String(s.currentPage - 1)] || 0) % 360;
    const vp = page.getViewport({ scale: 1, rotation: rot });

    const containerH = pdfContainer.clientHeight - 16;
    const fitW = pdfContainer.clientWidth - 16;
    const dualW = (pdfContainer.clientWidth - 20) / 2;

    const fitScale = Math.min(fitW / vp.width, containerH / vp.height);
    const dualScale = Math.min(dualW / vp.width, containerH / vp.height);

    s.displayMode = dualScale >= fitScale ? "2up" : "fit";
  }
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
    updateModeButtons();
    setTool("nav");
    renderPage();
  });

  btnZoomWide.addEventListener("click", () => {
    const s = getState();
    s.displayMode = "wide";
    s.userLockedMode = true;
    updateModeButtons();
    setTool("nav");
    renderPage();
  });

  btnSideBySide.addEventListener("click", () => {
    const s = getState();
    s.displayMode = "2up";
    s.userLockedMode = true;
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
        await checkAutoSideBySide();
        renderPage();
      }
    }, 150);
  });
}
