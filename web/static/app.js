/* ================================================================== */
/* MusicScoreViewer — Web frontend                                    */
/* ================================================================== */

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
const textDialog = $("#text-dialog");
const textDialogTitle = $("#text-dialog-title");
const textInput = $("#text-input");
const textFont = $("#text-font");
const textCancel = $("#text-cancel");

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

// Page layout info (set during render)
// Each entry: {page, cssW, cssH} — the CSS dimensions of each rendered page
let pageLayouts = [];

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function api(url, options = {}) {
  const resp = await fetch(url, options);
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
  titleDisplay.textContent = `${score.composer} — ${score.title}`;

  libraryView.classList.add("hidden");
  viewerView.classList.remove("hidden");
  btnLibrary.classList.add("hidden");
  btnBack.classList.remove("hidden");

  // Load annotations
  try {
    const data = await api(`/api/annotations?path=${encodeURIComponent(score.filepath)}`);
    annotations = data.pages || {};
    rotations = data.rotations || {};
  } catch {
    annotations = {};
    rotations = {};
  }
  undoStacks = {};

  try {
    const loadingTask = pdfjsLib.getDocument(`/api/pdf?path=${encodeURIComponent(score.filepath)}`);
    pdfDoc = await loadingTask.promise;
    totalPages = pdfDoc.numPages;
    pageTotal.textContent = totalPages;
    currentPage = 1;
    pageInput.max = totalPages;
    pageInput.value = 1;
    renderPage();
  } catch (err) {
    pdfContainer.innerHTML = `<p style="color:#f88;padding:20px">Failed to load PDF: ${esc(err.message)}</p>`;
  }
}

function closeScore() {
  pdfDoc = null;
  currentScore = null;
  totalPages = 0;
  currentPage = 1;
  annotations = {};
  rotations = {};
  undoStacks = {};
  pageLayouts = [];
  canvas1.width = 0;
  canvas1.height = 0;
  canvas2.width = 0;
  canvas2.height = 0;
  annotCanvas1.width = 0;
  annotCanvas1.height = 0;
  annotCanvas2.width = 0;
  annotCanvas2.height = 0;
  pageWrap2.classList.add("hidden");

  viewerView.classList.add("hidden");
  libraryView.classList.remove("hidden");
  btnBack.classList.add("hidden");
  btnLibrary.classList.remove("hidden");
  titleDisplay.textContent = "";
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
  } finally {
    rendering = false;
  }
}

async function renderSinglePage(pageNum, pdfCanvas, annotCanvas) {
  const page = await pdfDoc.getPage(pageNum);
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

function goToPage(n) {
  const p = Math.max(1, Math.min(totalPages, n));
  if (p !== currentPage) {
    currentPage = p;
    renderPage();
  }
}

function nextPage() {
  const step = sideBySide ? 2 : 1;
  goToPage(currentPage + step);
}

function prevPage() {
  const step = sideBySide ? 2 : 1;
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
  map[tool]?.classList.add("active");

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

async function saveAnnotations() {
  if (!currentScore) return;
  try {
    await api("/api/annotations", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: currentScore.filepath,
        pages: annotations,
        rotations: rotations,
      }),
    });
  } catch (err) {
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
  if (activeTool === "nav") return;
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

btnBack.addEventListener("click", closeScore);

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
  if (textDialog.open || dirDialog.open) return;

  if (!pdfDoc) return;

  // Tool shortcuts
  switch (e.key) {
    case "v": setTool("nav"); return;
    case "d": setTool("pen"); return;
    case "t": setTool("text"); return;
    case "e": setTool("eraser"); return;
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
  resizeTimer = setTimeout(() => {
    if (pdfDoc) renderPage();
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

(async function init() {
  try {
    const cfg = await api("/api/config");
    if (cfg.library_dir && cfg.score_count > 0) {
      // Re-use last folder
      dirInput.value = cfg.library_dir;
      await loadLibrary();
    } else {
      // No library configured (or empty) — prompt immediately
      dirInput.value = cfg.library_dir || "";
      dirDialog.showModal();
      dirInput.focus();
    }
  } catch (err) {
    libraryStatus.textContent = `Error: ${err.message}`;
  }
})();
