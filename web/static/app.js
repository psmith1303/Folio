/* ================================================================== */
/* MusicScoreViewer — Web frontend                                    */
/* ================================================================== */

// ---------------------------------------------------------------------------
// pdf.js setup
// ---------------------------------------------------------------------------

const { pdfjsLib } = globalThis;
pdfjsLib.GlobalWorkerOptions.workerSrc =
  "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.9.155/pdf.worker.min.mjs";

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
const titleDisplay = $("#title-display");
const dirDialog = $("#dir-dialog");
const dirInput = $("#dir-input");
const dirCancel = $("#dir-cancel");

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
  canvas1.width = 0;
  canvas1.height = 0;
  canvas2.width = 0;
  canvas2.height = 0;
  canvas2.classList.add("hidden");

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

  try {
    await renderSinglePage(currentPage, canvas1);

    if (sideBySide && currentPage + 1 <= totalPages) {
      canvas2.classList.remove("hidden");
      await renderSinglePage(currentPage + 1, canvas2);
    } else {
      canvas2.classList.add("hidden");
      canvas2.width = 0;
      canvas2.height = 0;
    }
  } finally {
    rendering = false;
  }
}

async function renderSinglePage(pageNum, canvas) {
  const page = await pdfDoc.getPage(pageNum);

  // Calculate scale to fit the container
  const containerHeight = pdfContainer.clientHeight - 16;
  const containerWidth = sideBySide
    ? (pdfContainer.clientWidth - 20) / 2
    : pdfContainer.clientWidth - 16;

  const unscaledViewport = page.getViewport({ scale: 1 });
  const scaleW = containerWidth / unscaledViewport.width;
  const scaleH = containerHeight / unscaledViewport.height;
  const scale = Math.min(scaleW, scaleH);

  const viewport = page.getViewport({ scale });
  const dpr = window.devicePixelRatio || 1;

  canvas.width = Math.floor(viewport.width * dpr);
  canvas.height = Math.floor(viewport.height * dpr);
  canvas.style.width = Math.floor(viewport.width) + "px";
  canvas.style.height = Math.floor(viewport.height) + "px";

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  await page.render({ canvasContext: ctx, viewport }).promise;
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
  // Don't intercept when typing in inputs
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") {
    // Allow Escape to blur inputs
    if (e.key === "Escape") {
      e.target.blur();
      e.preventDefault();
    }
    return;
  }

  if (!pdfDoc) return;

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
    if (cfg.library_dir) {
      dirInput.value = cfg.library_dir;
      await loadLibrary();
    } else {
      libraryStatus.textContent = 'Click "Set Folder" to choose your music library.';
    }
  } catch (err) {
    libraryStatus.textContent = `Error: ${err.message}`;
  }
})();
