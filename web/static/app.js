/* ================================================================== */
/* Folio — Web frontend                                               */
/* ================================================================== */

// ---------------------------------------------------------------------------
// Polyfills for older browsers (iPad Safari < 15.4)
// ---------------------------------------------------------------------------

// crypto.randomUUID — Safari 15.4+
if (typeof crypto !== "undefined" && !crypto.randomUUID) {
  crypto.randomUUID = function () {
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (v) => v.toString(16).padStart(2, "0")).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) +
      "-" + h.slice(16, 20) + "-" + h.slice(20);
  };
}

// <dialog> element — Safari 15.4+
// On older browsers, dialog.showModal / dialog.close / dialog.open don't exist.
(function polyfillDialog() {
  if (typeof HTMLDialogElement !== "undefined") return;

  document.querySelectorAll("dialog").forEach(function (dlg) {
    dlg.style.display = "none";

    // Backdrop overlay (inserted once per dialog)
    var backdrop = document.createElement("div");
    backdrop.className = "dialog-backdrop-polyfill";
    backdrop.style.cssText =
      "display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:999";
    dlg.parentNode.insertBefore(backdrop, dlg);
    dlg._backdrop = backdrop;

    dlg.returnValue = "";

    dlg.showModal = function () {
      this.returnValue = "";
      this.setAttribute("open", "");
      this.style.display = "block";
      this._backdrop.style.display = "block";
    };

    dlg.close = function (val) {
      if (val !== undefined) this.returnValue = val;
      this.removeAttribute("open");
      this.style.display = "none";
      this._backdrop.style.display = "none";
      this.dispatchEvent(new Event("close"));
    };

    Object.defineProperty(dlg, "open", {
      get: function () { return this.hasAttribute("open"); },
    });

    // Handle form[method=dialog] — track which submit button was clicked
    var form = dlg.querySelector('form[method="dialog"]');
    if (form) {
      var lastSubmitter = null;
      form.querySelectorAll('button[type="submit"]').forEach(function (btn) {
        btn.addEventListener("click", function () { lastSubmitter = btn; });
      });
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        dlg.returnValue = lastSubmitter ? (lastSubmitter.value || "") : "";
        lastSubmitter = null;
        dlg.close(dlg.returnValue);
      });
    }
  });
})();

// ---------------------------------------------------------------------------
// pdf.js setup (ES module import from CDN)
// ---------------------------------------------------------------------------

import * as pdfjsLib from "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/5.4.149/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc =
  "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/5.4.149/pdf.worker.min.mjs";

// Musical symbols that get enlarged (matches desktop app MUSICAL_SYMBOLS_SET)
const MUSICAL_SYMBOLS = new Set([
  "\u{1D15E}", "\u2669", "\u2669.", "\u266A",
  "pp", "p", "mp", "mf", "f", "ff",
  "sfz", "cresc", "dim",
]);

// ---------------------------------------------------------------------------
// DOM references
// ---------------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const libraryView = $("#library-view");
const viewerView = $("#viewer");
const btnLibrary = $("#btn-library");
const btnBack = $("#btn-back");
const btnSetDir = $("#btn-set-dir");
const searchInput = $("#search-input");
const composerFilter = $("#composer-filter");
const tagBar = $("#tag-bar");
const libraryBody = $("#library-body");
const libraryStatus = $("#library-status");
const btnPrev = $("#btn-prev");
const btnNext = $("#btn-next");
const pageInput = $("#page-input");
const pageTotal = $("#page-total");
const btnZoomFit = $("#btn-zoom-fit");
const btnSideBySide = $("#btn-side-by-side");
const pdfContainer = $("#pdf-container");
const canvas1 = $("#pdf-canvas");
const canvas2 = $("#pdf-canvas-2");
const annotCanvas1 = $("#annot-canvas");
const annotCanvas2 = $("#annot-canvas-2");
const pageWrap1 = $("#page-wrap-1");
const pageWrap2 = $("#page-wrap-2");
const titleDisplay = $("#title-display");
const dirDialog = $("#dir-dialog");
const dirInput = $("#dir-input");
const dirCancel = $("#dir-cancel");
const btnNav = $("#btn-nav");
const btnPen = $("#btn-pen");
const btnText = $("#btn-text");
const btnEraser = $("#btn-eraser");
const btnUndo = $("#btn-undo");
const sizeSlider = $("#size-slider");
const btnRotCCW = $("#btn-rot-ccw");
const btnRotCW = $("#btn-rot-cw");
const btnSetlists = $("#btn-setlists");
const setlistView = $("#setlist-view");
const setlistBody = $("#setlist-body");
const setlistStatus = $("#setlist-status");
const btnNewSetlist = $("#btn-new-setlist");
const setlistDetailActions = $("#setlist-detail-actions");
const setlistDetailName = $("#setlist-detail-name");
const setlistSongsBody = $("#setlist-songs-body");
const btnRenameSetlist = $("#btn-rename-setlist");
const btnAddSong = $("#btn-add-song");
const btnPlaySetlist = $("#btn-play-setlist");
const setlistNameDialog = $("#setlist-name-dialog");
const setlistNameDialogTitle = $("#setlist-name-dialog-title");
const setlistNameInput = $("#setlist-name-input");
const setlistNameCancel = $("#setlist-name-cancel");
const songPickerDialog = $("#song-picker-dialog");
const songSearch = $("#song-search");
const songPickerList = $("#song-picker-list");
const songStart = $("#song-start");
const songEnd = $("#song-end");
const songPickerCancel = $("#song-picker-cancel");
const songPickerAdd = $("#song-picker-add");
const btnTheme = $("#btn-theme");
const btnExport = $("#btn-export");
const btnFullscreen = $("#btn-fullscreen");
const loginDialog = $("#login-dialog");
const loginInput = $("#login-input");
const loginError = $("#login-error");
const textDialog = $("#text-dialog");
const textDialogTitle = $("#text-dialog-title");
const textInput = $("#text-input");
const textFont = $("#text-font");
const textCancel = $("#text-cancel");
const btnReset = $("#btn-reset");
const btnAddToSetlist = $("#btn-add-to-setlist");
const setlistPickerDialog = $("#setlist-picker-dialog");
const setlistPickerList = $("#setlist-picker-list");
const setlistPickerCancel = $("#setlist-picker-cancel");

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let scores = [];
let composers = [];
let tags = [];
let selectedTags = new Set();
let sortCol = "composer";
let sortDesc = false;

let pdfDoc = null;
let currentPage = 1;
let totalPages = 0;
let currentScore = null;
let sideBySide = false;
let rendering = false;

// Annotation state
let activeTool = "nav";   // "nav", "pen", "text", "eraser"
let penColor = "black";
let annotations = {};     // {pageNum: [annot, ...]}
let rotations = {};       // {pageNum: degrees}
let currentStroke = [];   // [{x, y}, ...] in CSS pixels relative to annot canvas
let undoStacks = {};      // {pageNum: [snapshot, ...]}
const UNDO_DEPTH = 20;

// Annotation etag (multi-user conflict detection)
let annotationEtag = null;

// Page cache for memory management
let cachedPages = new Map(); // pageNum -> pdf.js page object

// Setlist state
let currentView = "library";
let returnView = "library";
let setlistPlayback = null;    // null or {name, songs, index}
let editingSetlistName = null;
let editingSetlistSongs = [];
let pickerSelectedScore = null;
let setlistNameMode = "create"; // "create" or "rename"

// Page layout info (set during render)
// Each entry: {page, cssW, cssH} — the CSS dimensions of each rendered page
let pageLayouts = [];

// ---------------------------------------------------------------------------
// View management
// ---------------------------------------------------------------------------

function showView(view) {
  currentView = view;
  libraryView.classList.add("hidden");
  setlistView.classList.add("hidden");
  viewerView.classList.add("hidden");
  btnLibrary.classList.add("hidden");
  btnSetlists.classList.add("hidden");
  btnBack.classList.add("hidden");
  btnLibrary.classList.remove("active");
  btnSetlists.classList.remove("active");

  switch (view) {
    case "library":
      libraryView.classList.remove("hidden");
      btnLibrary.classList.remove("hidden");
      btnSetlists.classList.remove("hidden");
      btnLibrary.classList.add("active");
      titleDisplay.textContent = "";
      break;
    case "setlists":
      setlistView.classList.remove("hidden");
      btnLibrary.classList.remove("hidden");
      btnSetlists.classList.remove("hidden");
      btnSetlists.classList.add("active");
      titleDisplay.textContent = "";
      break;
    case "viewer":
      viewerView.classList.remove("hidden");
      btnBack.classList.remove("hidden");
      break;
  }
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function api(url, options = {}) {
  const resp = await fetch(url, options);
  if (resp.status === 401) {
    showLoginDialog();
    throw new Error("Authentication required");
  }
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json();
}

// ---------------------------------------------------------------------------
// Library
// ---------------------------------------------------------------------------

async function loadLibrary() {
  const params = new URLSearchParams();
  const q = searchInput.value.trim();
  if (q) params.set("q", q);
  const comp = composerFilter.value;
  if (comp) params.set("composer", comp);
  for (const t of selectedTags) {
    params.append("tag", t);
  }
  params.set("sort", sortCol);
  if (sortDesc) params.set("desc", "true");

  try {
    const data = await api(`/api/library?${params}`);
    scores = data.scores;
    composers = data.composers;
    tags = data.tags;
    renderLibrary();
    renderComposerFilter();
    renderTags();
    libraryStatus.textContent = `${data.total} scores`;
  } catch (err) {
    libraryStatus.textContent = `Error: ${err.message}`;
  }
}

function renderLibrary() {
  libraryBody.innerHTML = "";
  for (const s of scores) {
    const tr = document.createElement("tr");
    tr.dataset.filepath = s.filepath;
    tr.innerHTML = `
      <td title="${esc(s.composer)}">${esc(s.composer)}</td>
      <td title="${esc(s.title)}">${esc(s.title)}</td>
      <td title="${esc(s.tags.join(", "))}">${esc(s.tags.join(", "))}</td>
    `;
    tr.addEventListener("click", () => openScore(s));
    libraryBody.appendChild(tr);
  }
}

function renderComposerFilter() {
  const current = composerFilter.value;
  composerFilter.innerHTML = '<option value="">All Composers</option>';
  for (const c of composers) {
    const opt = document.createElement("option");
    opt.value = c;
    opt.textContent = c;
    if (c === current) opt.selected = true;
    composerFilter.appendChild(opt);
  }
}

function renderTags() {
  tagBar.innerHTML = "";
  for (const t of tags) {
    const chip = document.createElement("span");
    chip.className = "tag-chip" + (selectedTags.has(t) ? " selected" : "");
    chip.textContent = t;
    chip.addEventListener("click", () => {
      if (selectedTags.has(t)) {
        selectedTags.delete(t);
      } else {
        selectedTags.add(t);
      }
      loadLibrary();
    });
    tagBar.appendChild(chip);
  }
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// ---------------------------------------------------------------------------
// Sort
// ---------------------------------------------------------------------------

document.querySelectorAll("th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const col = th.dataset.col;
    if (sortCol === col) {
      sortDesc = !sortDesc;
    } else {
      sortCol = col;
      sortDesc = false;
    }
    updateSortHeaders();
    loadLibrary();
  });
});

function updateSortHeaders() {
  document.querySelectorAll("th.sortable").forEach((th) => {
    th.classList.remove("sort-asc", "sort-desc");
    const col = th.dataset.col;
    const base = col.charAt(0).toUpperCase() + col.slice(1);
    if (col === sortCol) {
      th.classList.add(sortDesc ? "sort-desc" : "sort-asc");
      th.textContent = base + (sortDesc ? " ▼" : " ▲");
    } else {
      th.textContent = base;
    }
  });
}

// ---------------------------------------------------------------------------
// PDF viewer
// ---------------------------------------------------------------------------

async function openScore(score) {
  currentScore = score;
  setlistPlayback = null;
  returnView = currentView;
  titleDisplay.textContent = `${score.composer} — ${score.title}`;
  showView("viewer");

  // Load annotations
  try {
    const data = await api(`/api/annotations?path=${encodeURIComponent(score.filepath)}`);
    annotations = data.pages || {};
    rotations = data.rotations || {};
    annotationEtag = data.etag || null;
  } catch {
    annotations = {};
    rotations = {};
    annotationEtag = null;
  }
  undoStacks = {};
  cachedPages.clear();

  try {
    const loadingTask = pdfjsLib.getDocument(`/api/pdf?path=${encodeURIComponent(score.filepath)}`);
    pdfDoc = await loadingTask.promise;
    totalPages = pdfDoc.numPages;
    pageTotal.textContent = totalPages;
    currentPage = 1;
    pageInput.max = totalPages;
    pageInput.value = 1;
    await autoSideBySide();
    renderPage();
  } catch (err) {
    pdfContainer.innerHTML = `<p style="color:#f88;padding:20px">Failed to load PDF: ${esc(err.message)}</p>`;
  }
}

async function autoSideBySide() {
  // Only use 2-up when two pages fit without reducing the zoom level.
  // Compare the fit-scale (full width for one page) with the dual-scale
  // (half width for each page).  If height is the limiting dimension in
  // both cases the scales are equal and 2-up is free.
  if (!pdfDoc || totalPages < 2 || currentPage >= totalPages) {
    sideBySide = false;
  } else {
    const page = await pdfDoc.getPage(currentPage);
    cachedPages.set(currentPage, page);
    const rot = (rotations[String(currentPage - 1)] || 0) % 360;
    const vp = page.getViewport({ scale: 1, rotation: rot });

    const containerH = pdfContainer.clientHeight - 16;
    const fitW = pdfContainer.clientWidth - 16;
    const dualW = (pdfContainer.clientWidth - 20) / 2;

    const fitScale = Math.min(fitW / vp.width, containerH / vp.height);
    const dualScale = Math.min(dualW / vp.width, containerH / vp.height);

    sideBySide = dualScale >= fitScale;
  }
  btnSideBySide.classList.toggle("active", sideBySide);
  btnZoomFit.classList.toggle("active", !sideBySide);
}

function closeScore() {
  cleanupAllPages();
  pdfDoc = null;
  currentScore = null;
  totalPages = 0;
  currentPage = 1;
  annotations = {};
  rotations = {};
  undoStacks = {};
  pageLayouts = [];
  annotationEtag = null;
  setlistPlayback = null;
  setTool("nav");
  canvas1.width = 0;
  canvas1.height = 0;
  canvas2.width = 0;
  canvas2.height = 0;
  annotCanvas1.width = 0;
  annotCanvas1.height = 0;
  annotCanvas2.width = 0;
  annotCanvas2.height = 0;
  pageWrap2.classList.add("hidden");
  titleDisplay.textContent = "";
  showView(returnView);
}

async function renderPage() {
  if (!pdfDoc || rendering) return;
  rendering = true;

  pageInput.value = currentPage;
  pageLayouts = [];

  try {
    const layout1 = await renderSinglePage(currentPage, canvas1, annotCanvas1);
    pageLayouts.push({ page: currentPage, ...layout1 });

    if (sideBySide && currentPage + 1 <= totalPages) {
      pageWrap2.classList.remove("hidden");
      const layout2 = await renderSinglePage(currentPage + 1, canvas2, annotCanvas2);
      pageLayouts.push({ page: currentPage + 1, ...layout2 });
    } else {
      pageWrap2.classList.add("hidden");
      canvas2.width = 0;
      canvas2.height = 0;
      annotCanvas2.width = 0;
      annotCanvas2.height = 0;
    }

    drawAnnotations();
    cleanupOldPages();
    prefetchNextPage();
  } finally {
    rendering = false;
  }
}

function cleanupOldPages() {
  // Keep currently displayed pages + neighbours; release the rest
  const hot = new Set();
  for (const layout of pageLayouts) {
    hot.add(layout.page);
    hot.add(layout.page + 1);
    if (layout.page > 1) hot.add(layout.page - 1);
  }
  for (const [num, page] of cachedPages) {
    if (!hot.has(num)) {
      page.cleanup();
      cachedPages.delete(num);
    }
  }
}

function cleanupAllPages() {
  for (const page of cachedPages.values()) {
    page.cleanup();
  }
  cachedPages.clear();
}

async function prefetchNextPage() {
  if (!pdfDoc) return;
  const step = sideBySide ? 2 : 1;
  const next = currentPage + step;
  if (next <= totalPages && !cachedPages.has(next)) {
    try {
      const page = await pdfDoc.getPage(next);
      cachedPages.set(next, page);
    } catch { /* ignore prefetch failures */ }
  }
}

async function renderSinglePage(pageNum, pdfCanvas, annotCanvas) {
  const page = await pdfDoc.getPage(pageNum);
  cachedPages.set(pageNum, page);
  const rot = (rotations[String(pageNum - 1)] || 0) % 360;

  // Calculate scale to fit the container
  const containerHeight = pdfContainer.clientHeight - 16;
  const containerWidth = sideBySide
    ? (pdfContainer.clientWidth - 20) / 2
    : pdfContainer.clientWidth - 16;

  const unscaledViewport = page.getViewport({ scale: 1, rotation: rot });
  const scaleW = containerWidth / unscaledViewport.width;
  const scaleH = containerHeight / unscaledViewport.height;
  const scale = Math.min(scaleW, scaleH);

  const viewport = page.getViewport({ scale, rotation: rot });
  const dpr = window.devicePixelRatio || 1;

  const cssW = Math.floor(viewport.width);
  const cssH = Math.floor(viewport.height);

  // PDF canvas
  pdfCanvas.width = Math.floor(viewport.width * dpr);
  pdfCanvas.height = Math.floor(viewport.height * dpr);
  pdfCanvas.style.width = cssW + "px";
  pdfCanvas.style.height = cssH + "px";

  const ctx = pdfCanvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  await page.render({ canvasContext: ctx, viewport }).promise;

  // Annotation overlay canvas (same size, CSS pixels for drawing)
  annotCanvas.width = Math.floor(cssW * dpr);
  annotCanvas.height = Math.floor(cssH * dpr);
  annotCanvas.style.width = cssW + "px";
  annotCanvas.style.height = cssH + "px";

  return { cssW, cssH };
}

function getPageRange() {
  if (!setlistPlayback) return { min: 1, max: totalPages };
  const song = setlistPlayback.songs[setlistPlayback.index];
  const min = Math.max(1, Math.min(song.start_page || 1, totalPages));
  const max = song.end_page ? Math.min(song.end_page, totalPages) : totalPages;
  return { min, max };
}

function goToPage(n) {
  const range = getPageRange();
  const p = Math.max(range.min, Math.min(range.max, n));
  if (p !== currentPage) {
    currentPage = p;
    renderPage();
  }
}

function nextPage() {
  const step = sideBySide ? 2 : 1;
  const range = getPageRange();
  if (currentPage + step > range.max) {
    if (setlistPlayback && setlistPlayback.index < setlistPlayback.songs.length - 1) {
      openSetlistSong(setlistPlayback.index + 1);
    }
    return;
  }
  goToPage(currentPage + step);
}

function prevPage() {
  const step = sideBySide ? 2 : 1;
  const range = getPageRange();
  if (currentPage - step < range.min) {
    if (setlistPlayback && setlistPlayback.index > 0) {
      openSetlistSong(setlistPlayback.index - 1, true);
    }
    return;
  }
  goToPage(currentPage - step);
}

// ---------------------------------------------------------------------------
// Annotation drawing
// ---------------------------------------------------------------------------

function drawAnnotations() {
  // Draw annotations on each visible page
  for (let i = 0; i < pageLayouts.length; i++) {
    const layout = pageLayouts[i];
    const ac = i === 0 ? annotCanvas1 : annotCanvas2;
    drawPageAnnotations(ac, layout);
  }
}

function drawPageAnnotations(annotCanvas, layout) {
  const dpr = window.devicePixelRatio || 1;
  const ctx = annotCanvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, layout.cssW, layout.cssH);

  const pg = String(layout.page - 1); // 0-indexed page key
  const pageAnnots = annotations[pg] || [];
  const rot = (rotations[pg] || 0) % 360;

  for (const annot of pageAnnots) {
    if (annot.type === "ink") {
      drawInk(ctx, annot, layout.cssW, layout.cssH, rot);
    } else if (annot.type === "text") {
      drawText(ctx, annot, layout.cssW, layout.cssH, rot);
    }
  }
}

function transformPt(nx, ny, w, h, rot) {
  // Annotation coords are in original page space; transform for current rotation
  if (rot === 90)  { [nx, ny] = [ny, 1.0 - nx]; }
  else if (rot === 180) { [nx, ny] = [1.0 - nx, 1.0 - ny]; }
  else if (rot === 270) { [nx, ny] = [1.0 - ny, nx]; }
  return [nx * w, ny * h];
}

function drawInk(ctx, annot, w, h, rot) {
  const pts = annot.points;
  if (!pts || pts.length < 2) return;

  ctx.beginPath();
  const [x0, y0] = transformPt(pts[0][0], pts[0][1], w, h, rot);
  ctx.moveTo(x0, y0);
  for (let i = 1; i < pts.length; i++) {
    const [x, y] = transformPt(pts[i][0], pts[i][1], w, h, rot);
    ctx.lineTo(x, y);
  }
  ctx.strokeStyle = annot.color || "black";
  ctx.lineWidth = annot.width || 2;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.stroke();
}

function drawText(ctx, annot, w, h, rot) {
  const [cx, cy] = transformPt(annot.x, annot.y, w, h, rot);
  let sz = 12 + (annot.size || 2) * 4;
  if (MUSICAL_SYMBOLS.has(annot.text)) {
    sz = Math.round(sz * 6);
  }
  const font = annot.font || "sans-serif";
  ctx.font = `${sz}px ${font}`;
  ctx.fillStyle = annot.color || "black";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(annot.text, cx, cy);
}

// ---------------------------------------------------------------------------
// Annotation tools
// ---------------------------------------------------------------------------

function setTool(tool) {
  activeTool = tool;
  document.querySelectorAll(".tool-btn").forEach((b) => b.classList.remove("active"));
  const map = { nav: btnNav, pen: btnPen, text: btnText, eraser: btnEraser };
  if (map[tool]) map[tool].classList.add("active");

  // Update cursor on annotation canvases
  for (const ac of [annotCanvas1, annotCanvas2]) {
    ac.classList.remove("tool-pen", "tool-text", "tool-eraser");
    if (tool !== "nav") ac.classList.add(`tool-${tool}`);
  }

  // Control touch behavior: allow scrolling in nav mode, prevent in drawing modes
  for (const ac of [annotCanvas1, annotCanvas2]) {
    ac.style.touchAction = tool === "nav" ? "auto" : "none";
  }
}

btnNav.addEventListener("click", () => setTool("nav"));
btnPen.addEventListener("click", () => setTool("pen"));
btnText.addEventListener("click", () => setTool("text"));
btnEraser.addEventListener("click", () => setTool("eraser"));

// Color swatches
document.querySelectorAll(".swatch").forEach((sw) => {
  sw.addEventListener("click", () => {
    document.querySelectorAll(".swatch").forEach((s) => s.classList.remove("selected"));
    sw.classList.add("selected");
    penColor = sw.dataset.color;
  });
});

// Undo
btnUndo.addEventListener("click", () => doUndo());

function pushUndo(pg) {
  if (!undoStacks[pg]) undoStacks[pg] = [];
  const snapshot = JSON.parse(JSON.stringify(annotations[pg] || []));
  undoStacks[pg].push(snapshot);
  if (undoStacks[pg].length > UNDO_DEPTH) {
    undoStacks[pg].shift();
  }
}

function doUndo() {
  const pg = String(currentPage - 1);
  const stack = undoStacks[pg];
  if (!stack || stack.length === 0) return;
  annotations[pg] = stack.pop();
  saveAnnotations();
  drawAnnotations();
}

// ---------------------------------------------------------------------------
// Page rotation
// ---------------------------------------------------------------------------

function rotatePage(delta) {
  if (!pdfDoc) return;
  const pg = String(currentPage - 1);
  const current = (rotations[pg] || 0) % 360;
  const next = (current + delta + 360) % 360;
  rotations[pg] = next;
  saveAnnotations();
  renderPage();
}

btnRotCW.addEventListener("click", () => rotatePage(90));
btnRotCCW.addEventListener("click", () => rotatePage(-90));

async function saveAnnotations(force = false) {
  if (!currentScore) return;
  try {
    const payload = {
      path: currentScore.filepath,
      pages: annotations,
      rotations: rotations,
    };
    if (!force && annotationEtag !== null) {
      payload.expected_etag = annotationEtag;
    }
    const result = await api("/api/annotations", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (result.etag) {
      annotationEtag = result.etag;
    }
  } catch (err) {
    if (err.message && err.message.includes("409")) {
      showConflictDialog();
      return;
    }
    console.error("Failed to save annotations:", err);
  }
}

// ---------------------------------------------------------------------------
// Pointer events on annotation canvases
// ---------------------------------------------------------------------------

function setupAnnotCanvas(annotCanvas, layoutIndex) {
  annotCanvas.addEventListener("pointerdown", (e) => onPointerDown(e, annotCanvas, layoutIndex));
  annotCanvas.addEventListener("pointermove", (e) => onPointerMove(e, annotCanvas, layoutIndex));
  annotCanvas.addEventListener("pointerup", (e) => onPointerUp(e, annotCanvas, layoutIndex));
}

setupAnnotCanvas(annotCanvas1, 0);
setupAnnotCanvas(annotCanvas2, 1);

function canvasCoords(e, annotCanvas) {
  const rect = annotCanvas.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

function onPointerDown(e, annotCanvas, layoutIndex) {
  if (activeTool === "nav") {
    // Click on right/bottom half → next page, left/top half → previous page
    const { x, y } = canvasCoords(e, annotCanvas);
    const layout = pageLayouts[layoutIndex];
    if (layout) {
      const rightHalf = x > layout.cssW / 2;
      const bottomHalf = y > layout.cssH / 2;
      if (rightHalf || bottomHalf) {
        nextPage();
      } else {
        prevPage();
      }
    }
    return;
  }
  e.preventDefault();

  const layout = pageLayouts[layoutIndex];
  if (!layout) return;

  if (activeTool === "pen") {
    const { x, y } = canvasCoords(e, annotCanvas);
    currentStroke = [{ x, y }];
    annotCanvas.setPointerCapture(e.pointerId);
  } else if (activeTool === "eraser") {
    eraseAt(e, annotCanvas, layoutIndex);
    annotCanvas.setPointerCapture(e.pointerId);
  } else if (activeTool === "text") {
    handleTextClick(e, annotCanvas, layoutIndex);
  }
}

function onPointerMove(e, annotCanvas, layoutIndex) {
  if (activeTool === "pen" && currentStroke.length > 0) {
    e.preventDefault();
    const { x, y } = canvasCoords(e, annotCanvas);
    currentStroke.push({ x, y });

    // Draw preview segment
    const dpr = window.devicePixelRatio || 1;
    const ctx = annotCanvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const prev = currentStroke[currentStroke.length - 2];
    ctx.beginPath();
    ctx.moveTo(prev.x, prev.y);
    ctx.lineTo(x, y);
    ctx.strokeStyle = penColor;
    ctx.lineWidth = parseInt(sizeSlider.value, 10);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.stroke();
  } else if (activeTool === "eraser" && e.buttons > 0) {
    e.preventDefault();
    eraseAt(e, annotCanvas, layoutIndex);
  }
}

function onPointerUp(e, annotCanvas, layoutIndex) {
  if (activeTool === "pen" && currentStroke.length > 1) {
    const layout = pageLayouts[layoutIndex];
    if (!layout) { currentStroke = []; return; }

    const pg = String(layout.page - 1);
    const rot = (rotations[pg] || 0) % 360;

    // Convert CSS pixels → normalized coords (undo display rotation)
    const norm = currentStroke.map(({ x, y }) => {
      let nx = x / layout.cssW;
      let ny = y / layout.cssH;
      // Inverse of transformPt: undo display rotation to get original page coords
      if (rot === 90)  { [nx, ny] = [1.0 - ny, nx]; }
      else if (rot === 180) { [nx, ny] = [1.0 - nx, 1.0 - ny]; }
      else if (rot === 270) { [nx, ny] = [ny, 1.0 - nx]; }
      return [nx, ny];
    });

    pushUndo(pg);
    if (!annotations[pg]) annotations[pg] = [];
    annotations[pg].push({
      uuid: crypto.randomUUID(),
      type: "ink",
      points: norm,
      color: penColor,
      width: parseInt(sizeSlider.value, 10),
    });
    saveAnnotations();
    drawAnnotations();
  }
  currentStroke = [];
}

// ---------------------------------------------------------------------------
// Eraser
// ---------------------------------------------------------------------------

function eraseAt(e, annotCanvas, layoutIndex) {
  const layout = pageLayouts[layoutIndex];
  if (!layout) return;

  const { x, y } = canvasCoords(e, annotCanvas);
  const pg = String(layout.page - 1);
  const pageAnnots = annotations[pg];
  if (!pageAnnots || pageAnnots.length === 0) return;

  const rot = (rotations[pg] || 0) % 360;
  const halo = 20; // pixels

  for (let i = pageAnnots.length - 1; i >= 0; i--) {
    const annot = pageAnnots[i];
    if (hitTest(annot, x, y, layout.cssW, layout.cssH, rot, halo)) {
      pushUndo(pg);
      pageAnnots.splice(i, 1);
      saveAnnotations();
      drawAnnotations();
      return;
    }
  }
}

function hitTest(annot, px, py, w, h, rot, halo) {
  if (annot.type === "ink") {
    for (const pt of annot.points) {
      const [cx, cy] = transformPt(pt[0], pt[1], w, h, rot);
      if (Math.abs(cx - px) < halo && Math.abs(cy - py) < halo) return true;
    }
    return false;
  } else if (annot.type === "text") {
    const [cx, cy] = transformPt(annot.x, annot.y, w, h, rot);
    let sz = 12 + (annot.size || 2) * 4;
    if (MUSICAL_SYMBOLS.has(annot.text)) sz = Math.round(sz * 6);
    const textHalo = Math.max(halo, sz);
    return Math.abs(cx - px) < textHalo && Math.abs(cy - py) < textHalo;
  }
  return false;
}

// ---------------------------------------------------------------------------
// Text tool
// ---------------------------------------------------------------------------

let pendingTextAnnot = null; // {pg, nx, ny, editUuid}

function handleTextClick(e, annotCanvas, layoutIndex) {
  const layout = pageLayouts[layoutIndex];
  if (!layout) return;

  const { x, y } = canvasCoords(e, annotCanvas);
  const pg = String(layout.page - 1);
  const rot = (rotations[pg] || 0) % 360;

  // Check if clicking on existing text annotation
  const pageAnnots = annotations[pg] || [];
  let editAnnot = null;
  for (let i = pageAnnots.length - 1; i >= 0; i--) {
    const a = pageAnnots[i];
    if (a.type === "text" && hitTest(a, x, y, layout.cssW, layout.cssH, rot, 10)) {
      editAnnot = a;
      break;
    }
  }

  if (editAnnot) {
    // Edit existing
    pendingTextAnnot = { pg, editUuid: editAnnot.uuid };
    textDialogTitle.textContent = "Edit Text";
    textInput.value = editAnnot.text;
    textFont.value = editAnnot.font || "sans-serif";
  } else {
    // New text — convert click to normalized coords (undo display rotation)
    let nx = x / layout.cssW;
    let ny = y / layout.cssH;
    if (rot === 90)  { [nx, ny] = [1.0 - ny, nx]; }
    else if (rot === 180) { [nx, ny] = [1.0 - nx, 1.0 - ny]; }
    else if (rot === 270) { [nx, ny] = [ny, 1.0 - nx]; }

    pendingTextAnnot = { pg, nx, ny, editUuid: null };
    textDialogTitle.textContent = "Add Text";
    textInput.value = "";
    textFont.value = "sans-serif";
  }

  textDialog.showModal();
  textInput.focus();
}

// Symbol buttons insert into text input
document.querySelectorAll(".sym-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const sym = btn.dataset.sym;
    const pos = textInput.selectionStart;
    textInput.value = textInput.value.slice(0, pos) + sym + textInput.value.slice(pos);
    textInput.focus();
    textInput.setSelectionRange(pos + sym.length, pos + sym.length);
  });
});

textCancel.addEventListener("click", () => {
  pendingTextAnnot = null;
  textDialog.close();
});

textDialog.addEventListener("close", () => {
  if (!pendingTextAnnot) return;
  const text = textInput.value.trim();
  if (!text) { pendingTextAnnot = null; return; }

  const { pg, nx, ny, editUuid } = pendingTextAnnot;
  pendingTextAnnot = null;

  pushUndo(pg);

  if (editUuid) {
    // Update existing annotation
    const pageAnnots = annotations[pg] || [];
    const existing = pageAnnots.find((a) => a.uuid === editUuid);
    if (existing) {
      existing.text = text;
      existing.font = textFont.value;
      existing.color = penColor;
      existing.size = parseInt(sizeSlider.value, 10);
    }
  } else {
    // Create new annotation
    if (!annotations[pg]) annotations[pg] = [];
    annotations[pg].push({
      uuid: crypto.randomUUID(),
      type: "text",
      x: nx,
      y: ny,
      text: text,
      font: textFont.value,
      color: penColor,
      size: parseInt(sizeSlider.value, 10),
    });
  }

  saveAnnotations();
  drawAnnotations();
});

// ---------------------------------------------------------------------------
// Navigation events
// ---------------------------------------------------------------------------

btnBack.addEventListener("click", () => {
  if (currentView === "viewer") {
    closeScore();
  }
});

btnPrev.addEventListener("click", prevPage);
btnNext.addEventListener("click", nextPage);

pageInput.addEventListener("change", () => {
  goToPage(parseInt(pageInput.value, 10) || 1);
});

btnZoomFit.addEventListener("click", () => {
  sideBySide = false;
  btnZoomFit.classList.add("active");
  btnSideBySide.classList.remove("active");
  renderPage();
});

btnSideBySide.addEventListener("click", () => {
  sideBySide = true;
  btnSideBySide.classList.add("active");
  btnZoomFit.classList.remove("active");
  renderPage();
});

// Keyboard navigation
document.addEventListener("keydown", (e) => {
  // Don't intercept when typing in inputs or dialogs
  const tag = e.target.tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") {
    if (e.key === "Escape") {
      e.target.blur();
      e.preventDefault();
    }
    return;
  }

  // Dialog open — don't handle
  if (textDialog.open || dirDialog.open || setlistNameDialog.open || songPickerDialog.open || setlistPickerDialog.open || loginDialog.open || conflictDialog.open) return;

  if (!pdfDoc) return;

  // Tool shortcuts
  switch (e.key) {
    case "v": setTool("nav"); return;
    case "d": setTool("pen"); return;
    case "t": setTool("text"); return;
    case "e": setTool("eraser"); return;
    case "f": toggleFullscreen(); return;
    case "s": showSetlistPicker(); return;
    case "r": rotatePage(90); return;
    case "R": rotatePage(-90); return;
  }

  // Undo
  if (e.key === "z" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    doUndo();
    return;
  }

  switch (e.key) {
    case "ArrowRight":
    case "ArrowDown":
    case " ":
    case "n":
    case "PageDown":
      e.preventDefault();
      nextPage();
      break;
    case "ArrowLeft":
    case "ArrowUp":
    case "Backspace":
    case "p":
    case "PageUp":
      e.preventDefault();
      prevPage();
      break;
    case "Home":
      e.preventDefault();
      goToPage(1);
      break;
    case "End":
      e.preventDefault();
      goToPage(totalPages);
      break;
    case "Escape":
      closeScore();
      break;
  }
});

// Resize handler — debounced
let resizeTimer = null;
window.addEventListener("resize", () => {
  if (resizeTimer) clearTimeout(resizeTimer);
  resizeTimer = setTimeout(async () => {
    if (pdfDoc) {
      await checkAutoSideBySide();
      renderPage();
    }
  }, 150);
});

// ---------------------------------------------------------------------------
// Filter events
// ---------------------------------------------------------------------------

let searchTimer = null;
searchInput.addEventListener("input", () => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(loadLibrary, 200);
});

composerFilter.addEventListener("change", loadLibrary);

btnReset.addEventListener("click", () => {
  searchInput.value = "";
  composerFilter.value = "";
  selectedTags.clear();
  sortCol = "composer";
  sortDesc = false;
  updateSortHeaders();
  loadLibrary();
});

// ---------------------------------------------------------------------------
// Setlist list view
// ---------------------------------------------------------------------------

btnLibrary.addEventListener("click", () => { showView("library"); loadLibrary(); });
btnSetlists.addEventListener("click", () => { showView("setlists"); loadSetlists(); });

async function loadSetlists() {
  try {
    const data = await api("/api/setlists");
    renderSetlistList(data.setlists);
    setlistStatus.textContent = `${data.setlists.length} setlists`;
  } catch (err) {
    setlistStatus.textContent = `Error: ${err.message}`;
  }
}

function renderSetlistList(setlists) {
  setlistBody.innerHTML = "";
  for (const sl of setlists) {
    const tr = document.createElement("tr");
    if (sl.name === editingSetlistName) {
      tr.classList.add("selected-setlist");
    }
    tr.innerHTML = `
      <td>${esc(sl.name)}</td>
      <td>${sl.count}</td>
      <td class="setlist-actions">
        <button class="small-btn del-btn" title="Delete">&#10005;</button>
      </td>
    `;
    // Single click selects and shows contents
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".del-btn")) return;
      openSetlistDetail(sl.name);
    });
    // Double-click starts playback
    const playSetlist = async () => {
      const detail = await api(`/api/setlists/${encodeURIComponent(sl.name)}`);
      if (detail.songs.length === 0) return;
      startSetlistPlayback(sl.name, detail.songs);
    };
    tr.addEventListener("dblclick", playSetlist);
    tr.querySelector(".del-btn").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete setlist "${sl.name}"?`)) return;
      await api(`/api/setlists/${encodeURIComponent(sl.name)}`, { method: "DELETE" });
      if (editingSetlistName === sl.name) {
        editingSetlistName = null;
        editingSetlistSongs = [];
        renderSetlistDetail();
      }
      loadSetlists();
    });
    setlistBody.appendChild(tr);
  }
}

// ---------------------------------------------------------------------------
// Setlist name dialog (create / rename)
// ---------------------------------------------------------------------------

btnNewSetlist.addEventListener("click", () => {
  setlistNameMode = "create";
  setlistNameDialogTitle.textContent = "New Setlist";
  setlistNameInput.value = "";
  setlistNameDialog.showModal();
  setlistNameInput.focus();
});

btnRenameSetlist.addEventListener("click", () => {
  setlistNameMode = "rename";
  setlistNameDialogTitle.textContent = "Rename Setlist";
  setlistNameInput.value = editingSetlistName || "";
  setlistNameDialog.showModal();
  setlistNameInput.focus();
});

setlistNameCancel.addEventListener("click", () => setlistNameDialog.close());

setlistNameDialog.addEventListener("close", async () => {
  if (setlistNameDialog.returnValue !== "ok") return;
  const name = setlistNameInput.value.trim();
  if (!name) return;

  try {
    if (setlistNameMode === "create") {
      await api("/api/setlists", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      loadSetlists();
    } else if (setlistNameMode === "rename" && editingSetlistName) {
      await api(`/api/setlists/${encodeURIComponent(editingSetlistName)}/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_name: name }),
      });
      editingSetlistName = name;
      setlistDetailName.textContent = name;
      loadSetlists();
    }
  } catch (err) {
    console.error("Setlist name operation failed:", err);
  }
});

// ---------------------------------------------------------------------------
// Setlist detail view
// ---------------------------------------------------------------------------

async function openSetlistDetail(name) {
  try {
    const data = await api(`/api/setlists/${encodeURIComponent(name)}`);
    editingSetlistName = data.name;
    editingSetlistSongs = data.songs;
    renderSetlistDetail();
    // Highlight the selected row in the left column
    setlistBody.querySelectorAll("tr").forEach((row) => {
      row.classList.toggle("selected-setlist",
        row.querySelector("td").textContent === name);
    });
  } catch (err) {
    console.error("Failed to load setlist:", err);
  }
}

function renderSetlistDetail() {
  setlistSongsBody.innerHTML = "";

  if (!editingSetlistName) {
    setlistDetailName.textContent = "Select a setlist";
    setlistDetailActions.classList.add("hidden");
    return;
  }

  setlistDetailName.textContent = editingSetlistName;
  setlistDetailActions.classList.remove("hidden");

  let dragSrcIndex = null;

  editingSetlistSongs.forEach((song, i) => {
    const tr = document.createElement("tr");
    tr.draggable = true;
    tr.dataset.index = i;
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td>${esc(song.composer || "")}</td>
      <td>${esc(song.title || "")}</td>
      <td><input type="number" class="page-input start-pg" min="1" value="${song.start_page || 1}"></td>
      <td><input type="number" class="page-input end-pg" min="0" value="${song.end_page || 0}"></td>
      <td class="song-actions">
        <button class="small-btn up-btn" title="Move up" ${i === 0 ? "disabled" : ""}>&#8593;</button>
        <button class="small-btn down-btn" title="Move down" ${i === editingSetlistSongs.length - 1 ? "disabled" : ""}>&#8595;</button>
        <button class="small-btn del-btn" title="Remove">&#10005;</button>
      </td>
    `;

    tr.querySelector(".start-pg").addEventListener("change", (e) => {
      song.start_page = parseInt(e.target.value, 10) || 1;
      saveSetlistSongs();
    });
    tr.querySelector(".end-pg").addEventListener("change", (e) => {
      const val = parseInt(e.target.value, 10) || 0;
      song.end_page = val === 0 ? null : val;
      saveSetlistSongs();
    });
    tr.querySelector(".up-btn").addEventListener("click", () => moveSong(i, -1));
    tr.querySelector(".down-btn").addEventListener("click", () => moveSong(i, 1));
    tr.querySelector(".del-btn").addEventListener("click", () => removeSong(i));

    // Drag-and-drop reorder
    tr.addEventListener("dragstart", (e) => {
      dragSrcIndex = i;
      tr.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    tr.addEventListener("dragend", () => {
      tr.classList.remove("dragging");
      setlistSongsBody.querySelectorAll("tr").forEach(
        (r) => r.classList.remove("drag-over")
      );
    });
    tr.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      setlistSongsBody.querySelectorAll("tr").forEach(
        (r) => r.classList.remove("drag-over")
      );
      tr.classList.add("drag-over");
    });
    tr.addEventListener("drop", (e) => {
      e.preventDefault();
      const destIndex = parseInt(tr.dataset.index, 10);
      if (dragSrcIndex !== null && dragSrcIndex !== destIndex) {
        const moved = editingSetlistSongs.splice(dragSrcIndex, 1)[0];
        editingSetlistSongs.splice(destIndex, 0, moved);
        saveSetlistSongs();
        renderSetlistDetail();
      }
    });

    setlistSongsBody.appendChild(tr);
  });
}

function moveSong(index, direction) {
  const newIndex = index + direction;
  if (newIndex < 0 || newIndex >= editingSetlistSongs.length) return;
  [editingSetlistSongs[index], editingSetlistSongs[newIndex]] =
    [editingSetlistSongs[newIndex], editingSetlistSongs[index]];
  saveSetlistSongs();
  renderSetlistDetail();
}

function removeSong(index) {
  editingSetlistSongs.splice(index, 1);
  saveSetlistSongs();
  renderSetlistDetail();
}

async function saveSetlistSongs() {
  if (!editingSetlistName) return;
  try {
    await api(`/api/setlists/${encodeURIComponent(editingSetlistName)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ songs: editingSetlistSongs }),
    });
  } catch (err) {
    console.error("Failed to save setlist:", err);
  }
}

// ---------------------------------------------------------------------------
// Song picker dialog
// ---------------------------------------------------------------------------

btnAddSong.addEventListener("click", async () => {
  pickerSelectedScore = null;
  songSearch.value = "";
  songStart.value = 1;
  songEnd.value = 0;
  songPickerAdd.disabled = true;
  await renderSongPicker("");
  songPickerDialog.showModal();
  songSearch.focus();
});

async function renderSongPicker(query) {
  const params = new URLSearchParams();
  if (query) params.set("q", query);
  try {
    const data = await api(`/api/library?${params}`);
    songPickerList.innerHTML = "";
    for (const s of data.scores) {
      const div = document.createElement("div");
      div.className = "picker-item";
      div.textContent = `${s.composer} — ${s.title}`;
      div.addEventListener("click", () => {
        songPickerList.querySelectorAll(".picker-item").forEach(
          (el) => el.classList.remove("selected")
        );
        div.classList.add("selected");
        pickerSelectedScore = s;
        songPickerAdd.disabled = false;
      });
      songPickerList.appendChild(div);
    }
  } catch (err) {
    songPickerList.innerHTML = `<p style="color:#f88">Error loading library</p>`;
  }
}

let songSearchTimer = null;
songSearch.addEventListener("input", () => {
  if (songSearchTimer) clearTimeout(songSearchTimer);
  songSearchTimer = setTimeout(() => renderSongPicker(songSearch.value.trim()), 200);
});

songPickerCancel.addEventListener("click", () => {
  pickerSelectedScore = null;
  songPickerDialog.close();
});

songPickerDialog.addEventListener("close", () => {
  if (songPickerDialog.returnValue !== "add" || !pickerSelectedScore) {
    pickerSelectedScore = null;
    return;
  }
  const startPage = parseInt(songStart.value, 10) || 1;
  const endVal = parseInt(songEnd.value, 10) || 0;

  editingSetlistSongs.push({
    path: pickerSelectedScore.filepath,
    title: pickerSelectedScore.title,
    composer: pickerSelectedScore.composer,
    start_page: startPage,
    end_page: endVal === 0 ? null : endVal,
  });

  pickerSelectedScore = null;
  saveSetlistSongs();
  renderSetlistDetail();
});

// ---------------------------------------------------------------------------
// Setlist playback
// ---------------------------------------------------------------------------

btnPlaySetlist.addEventListener("click", () => {
  if (editingSetlistSongs.length === 0) return;
  startSetlistPlayback(editingSetlistName, editingSetlistSongs);
});

function startSetlistPlayback(name, songs) {
  if (songs.length === 0) return;
  returnView = currentView;
  setlistPlayback = { name, songs, index: 0 };
  showView("viewer");
  openSetlistSong(0);
}

async function openSetlistSong(index, goToEnd = false) {
  setlistPlayback.index = index;
  const song = setlistPlayback.songs[index];
  const total = setlistPlayback.songs.length;

  currentScore = { filepath: song.path, composer: song.composer, title: song.title };
  titleDisplay.textContent = `${song.composer} — ${song.title} (${index + 1}/${total})`;

  // Load annotations
  try {
    const data = await api(`/api/annotations?path=${encodeURIComponent(song.path)}`);
    annotations = data.pages || {};
    rotations = data.rotations || {};
    annotationEtag = data.etag || null;
  } catch {
    annotations = {};
    rotations = {};
    annotationEtag = null;
  }
  undoStacks = {};
  cachedPages.clear();

  try {
    const loadingTask = pdfjsLib.getDocument(`/api/pdf?path=${encodeURIComponent(song.path)}`);
    pdfDoc = await loadingTask.promise;
    totalPages = pdfDoc.numPages;

    const range = getPageRange();
    pageTotal.textContent = totalPages;
    pageInput.max = totalPages;
    currentPage = goToEnd ? range.max : range.min;
    pageInput.value = currentPage;
    await autoSideBySide();
    renderPage();
  } catch (err) {
    pdfContainer.innerHTML = `<p style="color:#f88;padding:20px">Failed to load PDF: ${esc(err.message)}</p>`;
  }
}

// ---------------------------------------------------------------------------
// Set directory dialog
// ---------------------------------------------------------------------------

btnSetDir.addEventListener("click", () => {
  dirInput.value = "";
  dirDialog.showModal();
  dirInput.focus();
});

dirCancel.addEventListener("click", () => dirDialog.close());

dirDialog.addEventListener("close", async () => {
  const path = dirInput.value.trim();
  if (!path) return;
  try {
    libraryStatus.textContent = "Scanning…";
    await api("/api/library", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    selectedTags.clear();
    await loadLibrary();
  } catch (err) {
    libraryStatus.textContent = `Error: ${err.message}`;
  }
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Touch gestures (swipe navigation in nav mode)
// ---------------------------------------------------------------------------

let touchStartX = null;
let touchStartY = null;
const SWIPE_THRESHOLD = 50; // minimum px to register a swipe

for (const ac of [annotCanvas1, annotCanvas2]) {
  ac.addEventListener("touchstart", (e) => {
    if (activeTool !== "nav") return;
    if (e.touches.length !== 1) return;
    touchStartX = e.touches[0].clientX;
    touchStartY = e.touches[0].clientY;
  }, { passive: true });

  ac.addEventListener("touchend", (e) => {
    if (activeTool !== "nav" || touchStartX === null) return;
    if (e.changedTouches.length !== 1) return;
    const dx = e.changedTouches[0].clientX - touchStartX;
    const dy = e.changedTouches[0].clientY - touchStartY;
    touchStartX = null;
    touchStartY = null;

    // Only trigger if the horizontal swipe is dominant
    if (Math.abs(dx) > SWIPE_THRESHOLD && Math.abs(dx) > Math.abs(dy)) {
      if (dx < 0) nextPage();   // swipe left → next
      else prevPage();          // swipe right → prev
    }
  }, { passive: true });
}

// ---------------------------------------------------------------------------
// Auto side-by-side on wide screens
// ---------------------------------------------------------------------------

async function checkAutoSideBySide() {
  if (!pdfDoc) return;
  const prev = sideBySide;
  await autoSideBySide();
  // Only trigger re-render from the resize handler if the mode changed;
  // the resize handler already calls renderPage() unconditionally.
  return prev !== sideBySide;
}

// ---------------------------------------------------------------------------
// Theme toggle
// ---------------------------------------------------------------------------

// Migrate old localStorage key
if (!localStorage.getItem("folio-theme") && localStorage.getItem("msv-theme")) {
  localStorage.setItem("folio-theme", localStorage.getItem("msv-theme"));
  localStorage.removeItem("msv-theme");
}

const savedTheme = localStorage.getItem("folio-theme");
if (savedTheme === "light") {
  document.documentElement.classList.add("light");
  btnTheme.textContent = "Dark";
}

btnTheme.addEventListener("click", () => {
  const isLight = document.documentElement.classList.toggle("light");
  btnTheme.textContent = isLight ? "Dark" : "Light";
  localStorage.setItem("folio-theme", isLight ? "light" : "dark");
});

// ---------------------------------------------------------------------------
// Export annotated PDF
// ---------------------------------------------------------------------------

btnExport.addEventListener("click", async () => {
  if (!currentScore) return;
  try {
    btnExport.disabled = true;
    btnExport.textContent = "Exporting…";
    const resp = await fetch(
      `/api/pdf/export?path=${encodeURIComponent(currentScore.filepath)}`
    );
    if (!resp.ok) throw new Error(`Export failed: ${resp.status}`);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `annotated_${currentScore.filepath.split("/").pop()}`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    console.error("Export failed:", err);
  } finally {
    btnExport.disabled = false;
    btnExport.textContent = "Export";
  }
});

// ---------------------------------------------------------------------------
// Add to setlist (from viewer)
// ---------------------------------------------------------------------------

btnAddToSetlist.addEventListener("click", showSetlistPicker);

async function showSetlistPicker() {
  if (!currentScore) return;
  try {
    const data = await api("/api/setlists");
    setlistPickerList.innerHTML = "";
    if (data.setlists.length === 0) {
      setlistPickerList.innerHTML =
        '<p style="padding:10px;color:var(--fg-dim)">No setlists yet. Create one in the Setlists view.</p>';
    } else {
      for (const sl of data.setlists) {
        const div = document.createElement("div");
        div.className = "picker-item";
        div.textContent = `${sl.name} (${sl.count})`;
        div.addEventListener("click", () => addCurrentScoreToSetlist(sl.name));
        setlistPickerList.appendChild(div);
      }
    }
    setlistPickerDialog.showModal();
  } catch (err) {
    console.error("Failed to load setlists:", err);
  }
}

async function addCurrentScoreToSetlist(setlistName) {
  setlistPickerDialog.close();
  try {
    const data = await api(`/api/setlists/${encodeURIComponent(setlistName)}`);
    const songs = data.songs || [];
    songs.push({
      path: currentScore.filepath,
      title: currentScore.title || "",
      composer: currentScore.composer || "",
      start_page: 1,
      end_page: null,
    });
    await api(`/api/setlists/${encodeURIComponent(setlistName)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ songs }),
    });
    titleDisplay.textContent += ` — added to ${setlistName}`;
    setTimeout(() => {
      if (currentScore) {
        titleDisplay.textContent = `${currentScore.composer} — ${currentScore.title}`;
      }
    }, 2000);
  } catch (err) {
    console.error("Failed to add to setlist:", err);
  }
}

setlistPickerCancel.addEventListener("click", () => setlistPickerDialog.close());

// ---------------------------------------------------------------------------
// Fullscreen toggle
// ---------------------------------------------------------------------------

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen().catch(function () {});
  } else {
    document.exitFullscreen();
  }
}

btnFullscreen.addEventListener("click", toggleFullscreen);

document.addEventListener("fullscreenchange", () => {
  const fs = !!document.fullscreenElement;
  btnFullscreen.textContent = fs ? "Exit FS" : "Fullscreen";
  // In fullscreen, hide topbar and viewer toolbar — PDF only
  document.getElementById("topbar").classList.toggle("hidden", fs);
  document.getElementById("viewer-toolbar").classList.toggle("hidden", fs);
  // Re-render to fill the freed space
  if (pdfDoc) renderPage();
});

// ---------------------------------------------------------------------------
// Service worker
// ---------------------------------------------------------------------------

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch((err) => {
    console.warn("SW registration failed:", err);
  });
}

// ---------------------------------------------------------------------------
// Conflict dialog (multi-user awareness)
// ---------------------------------------------------------------------------

const conflictDialog = $("#conflict-dialog");
const conflictReload = $("#conflict-reload");
const conflictForce = $("#conflict-force");

function showConflictDialog() {
  conflictDialog.showModal();
}

conflictReload.addEventListener("click", async () => {
  conflictDialog.close();
  if (!currentScore) return;
  try {
    const data = await api(`/api/annotations?path=${encodeURIComponent(currentScore.filepath)}`);
    annotations = data.pages || {};
    rotations = data.rotations || {};
    annotationEtag = data.etag || null;
    undoStacks = {};
    renderPage();
  } catch (err) {
    console.error("Failed to reload annotations:", err);
  }
});

conflictForce.addEventListener("click", () => {
  conflictDialog.close();
  saveAnnotations(true);
});

// ---------------------------------------------------------------------------
// Login
// ---------------------------------------------------------------------------

function showLoginDialog() {
  loginInput.value = "";
  loginError.textContent = "";
  loginDialog.showModal();
  loginInput.focus();
}

loginDialog.addEventListener("close", async () => {
  if (loginDialog.returnValue !== "login") return;
  const passphrase = loginInput.value.trim();
  if (!passphrase) {
    showLoginDialog();
    return;
  }
  try {
    const resp = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passphrase }),
    });
    if (!resp.ok) {
      loginError.textContent = "Invalid passphrase";
      loginDialog.showModal();
      loginInput.focus();
      return;
    }
    // Re-run init on successful login
    initApp();
  } catch (err) {
    loginError.textContent = "Login failed: " + err.message;
    loginDialog.showModal();
    loginInput.focus();
  }
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function initApp() {
  try {
    const cfg = await api("/api/config");
    if (cfg.library_dir && cfg.score_count > 0) {
      dirInput.value = cfg.library_dir;
      await loadLibrary();
    } else {
      dirInput.value = cfg.library_dir || "";
      dirDialog.showModal();
      dirInput.focus();
    }
  } catch (err) {
    if (err.message === "Authentication required") return;
    libraryStatus.textContent = `Error: ${err.message}`;
  }
}

// Check auth status, then init
(async function () {
  try {
    const resp = await fetch("/api/auth-status");
    const status = await resp.json();
    if (status.auth_required && !status.authenticated) {
      showLoginDialog();
    } else {
      initApp();
    }
  } catch (err) {
    // Auth check failed — try init anyway
    initApp();
  }
})();
