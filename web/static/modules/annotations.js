// ---------------------------------------------------------------------------
// Annotation drawing, tools, pointer events, save/load, undo
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  annotCanvas1, annotCanvas2, sizeSlider, pdfContainer,
  btnPenStyle, penDialog, penGrid, penCancel,
  btnNav, btnPen, btnText, btnEraser, btnMove, btnStamp, btnStartPage, btnPencilOnly, btnUndo,
  btnRotCCW, btnRotCW,
} from "./dom.js";
import { api } from "./api.js";
import {
  NOTE_GLYPHS, UNDO_DEPTH, transformPt, inverseTransformPt, sizeToPt,
} from "./utils.js";
import { getStampImage, stampCursorPng, getStampMeta } from "./stamps.js";
import { showConflictDialog, showTextDialog } from "./dialog-handlers.js";
import { renderPage, invalidatePrerender, nextPage, prevPage, showToast } from "./viewer.js";
import {
  allPending, deletePending, getPending, isOffline, mergeAnnotations, putPending, syncEntry,
} from "./annot-outbox.js";

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

export function drawAnnotations() {
  const s = getState();
  for (let i = 0; i < s.pageLayouts.length; i++) {
    const layout = s.pageLayouts[i];
    const ac = i === 0 ? annotCanvas1 : annotCanvas2;
    drawPageAnnotations(ac, layout, i);
  }
}

// For bursty callers (drags, image loads): redraw at most once per frame.
let _drawScheduled = false;

function scheduleDraw() {
  if (_drawScheduled) return;
  _drawScheduled = true;
  requestAnimationFrame(() => {
    _drawScheduled = false;
    drawAnnotations();
  });
}

// Also draws the pen stroke in progress, if it's on this page, on top: the
// live stroke is the page redrawn with it (not segments added one by one),
// so it looks exactly as it will once committed, even when translucent.
function drawPageAnnotations(annotCanvas, layout, layoutIndex) {
  const s = getState();
  const dpr = window.devicePixelRatio || 1;
  const ctx = annotCanvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, layout.cssW, layout.cssH);

  const pg = String(layout.page - 1);
  const pageAnnots = s.annotations[pg] || [];
  const rot = (s.rotations[pg] || 0) % 360;

  for (const annot of pageAnnots) {
    const type = ANNOT_TYPES[annot.type];
    if (type) type.draw(ctx, annot, layout.cssW, layout.cssH, rot, layout.pdfW);
  }

  if (s.currentStroke.length > 1 && s.strokeLayoutIndex === layoutIndex) {
    const { widthPt, opacity } = penStyleAt(s.penStyle.row, s.penStyle.col);
    strokeLine(ctx, s.currentStroke.map(({ x, y }) => [x, y]), s.penColor,
      inkCssWidth({ widthPt }, layout.cssW, layout.pdfW), opacity);
  }
}

// Start-page stamp: a fixed-colour raster, always 20mm square on the page
// regardless of zoom (72pt per inch, 25.4mm per inch).
const START_STAMP_PT = 20 / 25.4 * 72;
const START_STAMP_SRC = "/stamps/start-page.png";
let _startStampImg = null;

// Returns the loaded image, or null until it's ready (then redraws and
// re-applies the start-page cursor).
function startStampImage() {
  if (!_startStampImg) {
    _startStampImg = new Image();
    _startStampImg.onload = () => {
      scheduleDraw();
      if (getState().activeTool === "startpage") setTool("startpage");
    };
    _startStampImg.src = START_STAMP_SRC;
  }
  return _startStampImg.complete && _startStampImg.naturalWidth ? _startStampImg : null;
}

function startStampCssSize(cssW, pdfW) {
  return START_STAMP_PT * (pdfW ? cssW / pdfW : 1);
}

function drawStartStamp(ctx, annot, w, h, rot, pdfW) {
  const [cx, cy] = transformPt(annot.x, annot.y, w, h, rot);
  const sz = startStampCssSize(w, pdfW);
  const img = startStampImage();
  if (!img) return;
  ctx.drawImage(img, cx - sz / 2, cy - sz / 2, sz, sz);
}

// On-screen stamp width/height in CSS px. The stamp's SMuFL width/height (in
// staff spaces, from the manifest) times the slider's points-per-staff-space,
// scaled to CSS px by the page render scale (cssW / pdfW). This preserves true
// SMuFL proportions (a repeat barline is tall, a crescendo short and wide).
function stampCssSize(stampId, sizeVal, cssW, pdfW) {
  const meta = getStampMeta(stampId);
  const cssScale = pdfW ? cssW / pdfW : 1;
  const pt = sizeToPt(sizeVal);
  return {
    wCss: pt * (meta ? meta.w : 1) * cssScale,
    hCss: pt * (meta ? meta.h : 1) * cssScale,
  };
}

function drawStamp(ctx, annot, w, h, rot, pdfW) {
  const [cx, cy] = transformPt(annot.x, annot.y, w, h, rot);
  const { wCss, hCss } = stampCssSize(annot.id, annot.size, w, pdfW);
  const color = annot.color || "black";
  // getStampImage returns null until the SVG raster is ready; the onReady
  // callback redraws once it loads (first paint of a freshly-loaded stamp).
  // Each redraw before then adds another callback, so coalesce them.
  const img = getStampImage(annot.id, color, scheduleDraw);
  if (!img) return;
  ctx.drawImage(img, cx - wCss / 2, cy - hCss / 2, wCss, hCss);
}

// A polyline in CSS px, stroked as one path so a translucent line doesn't
// darken where its segments overlap. Committed ink and the live pen stroke
// both draw through here.
function strokeLine(ctx, pts, color, lineWidth, opacity) {
  ctx.save();
  ctx.globalAlpha = opacity;
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  ctx.strokeStyle = color;
  ctx.lineWidth = lineWidth;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.stroke();
  ctx.restore();
}

// On-screen ink width in CSS px. Strokes with widthPt (PDF points) scale
// with the page, like text and stamps; older strokes stored screen px in
// `width` and draw exactly as they always have.
function inkCssWidth(annot, w, pdfW) {
  return annot.widthPt && pdfW ? annot.widthPt * w / pdfW : (annot.width || 2);
}

function drawInk(ctx, annot, w, h, rot, pdfW) {
  const pts = annot.points;
  if (!pts || pts.length < 2) return;
  strokeLine(ctx, pts.map(([x, y]) => transformPt(x, y, w, h, rot)),
    annot.color || "black", inkCssWidth(annot, w, pdfW), annot.opacity ?? 1);
}

// On-screen text size in CSS px: the slider's point size scaled by the page
// render scale, with note glyphs enlarged 6x. Shared by draw and hit-test so
// the eraser/move target always matches what is drawn.
function textCssSize(annot, w, pdfW) {
  const sz = sizeToPt(annot.size) * (pdfW ? w / pdfW : 1);
  return NOTE_GLYPHS.has(annot.text) ? Math.round(sz * 6) : sz;
}

// Where a text annotation's lines go, in CSS px: the anchor (bottom-left of
// the first line), font size, lines and line height. Shared by draw and
// hit-test, like textCssSize, so the eraser/move target matches the drawing.
function textLayout(annot, w, h, rot, pdfW) {
  const [cx, cy] = transformPt(annot.x, annot.y, w, h, rot);
  const sz = textCssSize(annot, w, pdfW);
  return { cx, cy, sz, lines: String(annot.text).split("\n"), lineH: sz * 1.2 };
}

function drawText(ctx, annot, w, h, rot, pdfW) {
  const { cx, cy, sz, lines, lineH } = textLayout(annot, w, h, rot, pdfW);
  const font = annot.font || "sans-serif";
  ctx.font = `${sz}px ${font}`;
  ctx.fillStyle = annot.color || "black";
  ctx.textAlign = "left";
  ctx.textBaseline = "bottom";
  for (let i = 0; i < lines.length; i++) {
    ctx.fillText(lines[i], cx, cy + i * lineH);
  }
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

export function setTool(tool) {
  const s = getState();
  s.activeTool = tool;
  if (tool !== "stamp") s.selectedStamp = null;
  document.querySelectorAll(".tool-btn").forEach((b) => b.classList.remove("active"));
  const map = {
    nav: btnNav, pen: btnPen, text: btnText, eraser: btnEraser,
    move: btnMove, stamp: btnStamp, startpage: btnStartPage,
  };
  if (map[tool]) map[tool].classList.add("active");

  const placement = PLACEMENT_TOOLS[tool];
  const stampCursor = placement ? placement.cursor(s) : "";

  for (const ac of [annotCanvas1, annotCanvas2]) {
    ac.classList.remove("tool-pen", "tool-text", "tool-eraser", "tool-move", "tool-stamp",
      "tool-startpage");
    if (tool !== "nav") ac.classList.add(`tool-${tool}`);
    ac.style.touchAction = tool === "nav" ? "auto" : "none";
    ac.style.cursor = stampCursor;
  }
}

// Cursor image for start-page mode, rendered at the stamp's on-screen size.
// Returns null until the image has loaded (then re-applies the tool).
function startStampCursorPng(sz) {
  const img = startStampImage();
  if (!img) return null;
  const c = document.createElement("canvas");
  c.width = c.height = sz;
  c.getContext("2d").drawImage(img, 0, 0, sz, sz);
  return c.toDataURL("image/png");
}

// Tools that place one mark on the next tap, then return to Nav. Escape and
// off-page clicks cancel them. For each:
//   cursor(state) -> CSS cursor for desktop: the mark itself at its on-screen
//     size, hotspot centred ("" for none). A PNG (canvas-rendered) cursor is
//     used because SVG-data-URI cursors render unreliably in some browsers
//     (Firefox). Touch devices have no cursor, so the tool is just "armed"
//     and the next tap places it. Browsers cap cursor size (~128px).
//   place(state, point) -> add the mark at `point` (from pagePoint); returns
//     false if there was nothing to place.
const PLACEMENT_TOOLS = {
  stamp: { cursor: stampToolCursor, place: placeStamp },
  startpage: { cursor: startPageToolCursor, place: placeStartPage },
};

export function isPlacementTool(tool) {
  return Object.hasOwn(PLACEMENT_TOOLS, tool);
}

function stampToolCursor(s) {
  if (!s.selectedStamp) return "";
  const layout = s.pageLayouts[0];
  let { wCss, hCss } = stampCssSize(s.selectedStamp, parseInt(sizeSlider.value, 10),
    layout ? layout.cssW : 0, layout ? layout.pdfW : 0);
  if (!(wCss > 0) || !(hCss > 0)) { wCss = hCss = 24; }  // no page rendered yet
  const m = Math.max(wCss, hCss);
  if (m > 128) { const k = 128 / m; wCss *= k; hCss *= k; }  // browser cursor cap
  // Re-apply the cursor once the stamp image finishes loading.
  const url = stampCursorPng(s.selectedStamp, s.penColor, wCss, hCss, () => {
    if (getState().activeTool === "stamp") setTool("stamp");
  });
  return url
    ? `url("${url}") ${Math.round(wCss / 2)} ${Math.round(hCss / 2)}, crosshair`
    : "crosshair";
}

function startPageToolCursor(s) {
  const layout = s.pageLayouts[0];
  let sz = layout ? startStampCssSize(layout.cssW, layout.pdfW) : 0;
  if (!(sz > 0)) sz = 24;
  sz = Math.round(Math.min(sz, 128));  // browser cursor cap
  const url = startStampCursorPng(sz);
  return url ? `url("${url}") ${sz >> 1} ${sz >> 1}, crosshair` : "crosshair";
}

// Enter stamp-placement mode with the given stamp id (called from the palette).
export function enterStampMode(stampId) {
  getState().selectedStamp = stampId;
  setTool("stamp");
}

// ---------------------------------------------------------------------------
// Undo
// ---------------------------------------------------------------------------

function pushUndo(pg) {
  const s = getState();
  if (!s.undoStacks[pg]) s.undoStacks[pg] = [];
  const snapshot = JSON.parse(JSON.stringify(s.annotations[pg] || []));
  s.undoStacks[pg].push(snapshot);
  if (s.undoStacks[pg].length > UNDO_DEPTH) {
    s.undoStacks[pg].shift();
  }
}

export function doUndo() {
  const s = getState();
  const pg = String(s.currentPage - 1);
  const stack = s.undoStacks[pg];
  if (!stack || stack.length === 0) return;
  s.annotations[pg] = stack.pop();
  saveAnnotations();
  drawAnnotations();
}

// ---------------------------------------------------------------------------
// Clear all annotations on the current page (undoable)
// ---------------------------------------------------------------------------

export function clearCurrentPageAnnotations() {
  const s = getState();
  if (!s.pdfDoc) return false;
  const pg = String(s.currentPage - 1);
  const pageAnnots = s.annotations[pg];
  if (!pageAnnots || pageAnnots.length === 0) return false;
  pushUndo(pg);
  s.annotations[pg] = [];
  saveAnnotations();
  drawAnnotations();
  return true;
}

// ---------------------------------------------------------------------------
// Pencil-only mode (palm rejection for the pen tool)
// ---------------------------------------------------------------------------

const PENCIL_ONLY_STORAGE_KEY = "folio.pencilOnly";

export function setPencilOnly(enabled) {
  const s = getState();
  s.pencilOnly = !!enabled;
  btnPencilOnly.classList.toggle("active", s.pencilOnly);
  try {
    localStorage.setItem(PENCIL_ONLY_STORAGE_KEY, s.pencilOnly ? "1" : "0");
  } catch (_) {
    // localStorage may be unavailable (private mode); ignore.
  }
}

function loadPencilOnlyPref() {
  try {
    return localStorage.getItem(PENCIL_ONLY_STORAGE_KEY) === "1";
  } catch (_) {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Pen style: a grid of widths (rows) x transparencies (columns)
// ---------------------------------------------------------------------------

// Widths in PDF points, so a stroke keeps its size relative to the music at
// any zoom. The highlighter column is wider: a highlight has to cover notes,
// and a thin translucent line would be all but invisible.
const PEN_WIDTHS_PT = [0.75, 1.5, 3, 6];
const PEN_OPACITIES = [1, 0.6, 0.3];
const PEN_ROW_LABELS = ["Fine", "Medium", "Bold", "Heavy"];
const PEN_COL_LABELS = ["Solid", "Semi", "Highlight"];
const HIGHLIGHT_COL = 2;
const HIGHLIGHT_WIDTH_FACTOR = 4;
const DEFAULT_PEN_STYLE = { row: 1, col: 0 };
const PEN_STYLE_STORAGE_KEY = "folio.penStyle";

// Grid previews draw widths at their true on-page size, capped so the
// heaviest highlighter still fits its cell; the toolbar chip is smaller.
const PEN_CELL_MAX_PX = 28;
const PEN_CHIP_MAX_PX = 10;

function penStyleAt(row, col) {
  const k = col === HIGHLIGHT_COL ? HIGHLIGHT_WIDTH_FACTOR : 1;
  return { widthPt: PEN_WIDTHS_PT[row] * k, opacity: PEN_OPACITIES[col] };
}

// A committed pen stroke. `width` is the stroke's on-screen px when drawn,
// kept so clients that predate widthPt still draw it about right; opacity
// is left out when solid, as on every stroke before it existed.
function inkAnnotation(points, color, penStyle, layout) {
  const { widthPt, opacity } = penStyleAt(penStyle.row, penStyle.col);
  const annot = {
    uuid: crypto.randomUUID(),
    type: "ink",
    points,
    color,
    widthPt,
    width: Math.max(1, Math.round(inkCssWidth({ widthPt }, layout.cssW, layout.pdfW))),
  };
  if (opacity < 1) annot.opacity = opacity;
  return annot;
}

function isPenStyle(v) {
  return !!v && Number.isInteger(v.row) && Number.isInteger(v.col)
    && v.row >= 0 && v.row < PEN_WIDTHS_PT.length
    && v.col >= 0 && v.col < PEN_OPACITIES.length;
}

function loadPenStylePref() {
  try {
    const v = JSON.parse(localStorage.getItem(PEN_STYLE_STORAGE_KEY));
    if (isPenStyle(v)) return { row: v.row, col: v.col };
  } catch (_) {
    // Unavailable storage or a garbled value: use the default.
  }
  return { ...DEFAULT_PEN_STYLE };
}

function setPenStyle(row, col) {
  getState().penStyle = { row, col };
  try {
    localStorage.setItem(PEN_STYLE_STORAGE_KEY, JSON.stringify({ row, col }));
  } catch (_) {
    // localStorage may be unavailable (private mode); ignore.
  }
  updatePenChip();
}

// CSS px per PDF point on the page as currently shown (1 before any page).
function pageCssPerPt(s) {
  const layout = s.pageLayouts[0];
  return layout && layout.pdfW ? layout.cssW / layout.pdfW : 1;
}

function penSampleSvg(color, widthPx, opacity, len, height) {
  const pad = widthPx / 2 + 2;
  return `<svg width="${len}" height="${height}" aria-hidden="true">`
    + `<line x1="${pad}" y1="${height / 2}" x2="${len - pad}" y2="${height / 2}" `
    + `stroke="${color}" stroke-width="${widthPx}" stroke-opacity="${opacity}" `
    + `stroke-linecap="round"/></svg>`;
}

function penSample(s, row, col, maxPx, len, height) {
  const { widthPt, opacity } = penStyleAt(row, col);
  const px = Math.max(1, Math.min(maxPx, widthPt * pageCssPerPt(s)));
  return penSampleSvg(s.penColor, px, opacity, len, height);
}

function updatePenChip() {
  const s = getState();
  const { row, col } = s.penStyle;
  btnPenStyle.innerHTML = penSample(s, row, col, PEN_CHIP_MAX_PX, 36, 14);
  btnPenStyle.title = `Pen: ${PEN_ROW_LABELS[row]}, ${PEN_COL_LABELS[col]}`;
}

function renderPenGrid() {
  const s = getState();
  const head = PEN_COL_LABELS.map((l) => `<th scope="col">${l}</th>`).join("");
  const rows = PEN_ROW_LABELS.map((rowLabel, row) => {
    const cells = PEN_COL_LABELS.map((colLabel, col) => {
      const sel = row === s.penStyle.row && col === s.penStyle.col;
      return `<td><button type="button" class="pen-cell${sel ? " selected" : ""}" `
        + `data-row="${row}" data-col="${col}" title="${rowLabel}, ${colLabel}" `
        + `aria-pressed="${sel}">${penSample(s, row, col, PEN_CELL_MAX_PX, 56, 32)}</button></td>`;
    }).join("");
    return `<tr><th scope="row">${rowLabel}</th>${cells}</tr>`;
  }).join("");
  penGrid.innerHTML = `<tr><th></th>${head}</tr>${rows}`;
}

// ---------------------------------------------------------------------------
// Page rotation
// ---------------------------------------------------------------------------

export function rotatePage(delta) {
  const s = getState();
  if (!s.pdfDoc) return;
  const pg = String(s.currentPage - 1);
  const current = (s.rotations[pg] || 0) % 360;
  const next = (current + delta + 360) % 360;
  s.rotations[pg] = next;
  saveAnnotations();
  invalidatePrerender(s.currentPage);
  renderPage();
}

// ---------------------------------------------------------------------------
// Save annotations
// ---------------------------------------------------------------------------

// Serialize saves so each one waits for the previous to complete,
// preventing false etag conflicts from concurrent in-flight requests.
let _saveChain = Promise.resolve();
// The save waiting behind the one in flight, if any. Saves send the whole
// current state, so further calls for the same score just ride along with
// it instead of queuing another full upload each.
let _queuedSave = null;

export function saveAnnotations(force = false) {
  const filepath = getState().currentScore?.filepath;
  if (_queuedSave && _queuedSave.filepath === filepath) {
    _queuedSave.force ||= force;
    return;
  }
  const job = { filepath, force };
  _queuedSave = job;
  _saveChain = _saveChain.then(() => {
    if (_queuedSave === job) _queuedSave = null;  // started: later edits need a new save
    const s = getState();
    if (!s.currentScore || s.currentScore.filepath !== filepath) return;
    return _doSaveAnnotations(s, job.force);
  }).catch((err) => {
    console.error("Save chain error:", err);
  });
}

// The score's annotation state as a save sends it (a copy).
function currentState(s) {
  return structuredClone({ pages: s.annotations, rotations: s.rotations });
}

// An outbox entry for the open score: `state`, and the server state it's
// based on.
function outboxEntry(s, state) {
  return { path: s.currentScore.filepath, base: s.annotationBase,
           baseEtag: s.annotationEtag, ...state };
}

// After a successful save or sync: `saved` ({pages, rotations}) is now the
// server's state, with `etag`. Edits made meanwhile are kept.
function markSaved(s, etag, saved) {
  s.annotationEtag = etag;
  s.annotationBase = saved;
}

async function _doSaveAnnotations(s, force) {
  const path = s.currentScore.filepath;
  // Offline edits not yet on the server, or a base we never saw (opened
  // offline with nothing cached): save through the outbox, which merges
  // with the server's annotations instead of overwriting them.
  if (await getPending(path) || (s.annotationEtag === null && !force)) {
    return syncOpenScore(s);
  }
  const sent = currentState(s);
  try {
    const payload = { path, ...sent };
    if (!force && s.annotationEtag !== null) {
      payload.expected_etag = s.annotationEtag;
    }
    const result = await api("/api/annotations", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (result.etag) markSaved(s, result.etag, sent);
  } catch (err) {
    if (isOffline(err)) {                     // keep it for later
      await putPending(outboxEntry(s, sent));
      noteOffline(s);
      return;
    }
    if (err.message && err.message.includes("409")) {
      showConflictDialog();
      return;
    }
    console.error("Failed to save annotations:", err);
  }
}

function noteOffline(s) {
  if (s.annotationsOffline) return;
  s.annotationsOffline = true;
  showToast("Offline — annotations are kept on this device and saved when back online",
            { duration: 6000 });
}

// Save the open score's annotations through the outbox: store them, then
// sync, merging with the server's copy if it changed. Strokes drawn while
// the sync was in flight are merged back on top of the result.
async function syncOpenScore(s) {
  const path = s.currentScore.filepath;
  const sent = currentState(s);
  const entry = outboxEntry(s, sent);
  await putPending(entry);
  let result;
  try {
    result = await syncEntry(entry);
  } catch (err) {
    console.error("Failed to sync offline annotations:", err);
    return;
  }
  if (!result) {
    noteOffline(s);
    return;
  }
  if (s.currentScore?.filepath === path) applySynced(s, sent, result);
}

// `sent` (the open score's annotations as synced) reached the server as
// `result` ({etag, pages, rotations}, merged with changes made elsewhere):
// adopt it, keeping strokes drawn since `sent`.
function applySynced(s, sent, result) {
  const server = { pages: result.pages, rotations: result.rotations };
  const local = currentState(s);
  const now = mergeAnnotations(sent, local, server);
  markSaved(s, result.etag, server);
  if (JSON.stringify(now) !== JSON.stringify(local)) {
    s.annotations = now.pages;
    s.rotations = now.rotations;
    s.undoStacks = {};     // undo snapshots predate the merge
    renderPage();
  }
  if (JSON.stringify(now) !== JSON.stringify(server)) saveAnnotations();
  if (s.annotationsOffline) {
    s.annotationsOffline = false;
    showToast("Offline annotations saved");
  }
}

// Sync every score's offline annotations (on launch and on reconnecting).
// The open score's go through its save queue, so they can't race its saves.
export async function syncPendingAnnotations() {
  let entries;
  try {
    entries = await allPending();
  } catch (err) {
    console.error("Can't read offline annotations:", err);
    return;
  }
  for (const entry of entries) {
    if (entry.path === getState().currentScore?.filepath) {
      saveAnnotations();
      continue;
    }
    _saveChain = _saveChain.then(async () => {
      const result = await syncEntry(entry);
      // Opened while this was queued: the score shows `entry`, so bring it
      // up to date, or its next save would conflict with this one.
      const s = getState();
      if (result && s.currentScore?.filepath === entry.path) {
        applySynced(s, { pages: entry.pages, rotations: entry.rotations }, result);
      }
    }).catch((err) => {
      console.error(`Failed to sync offline annotations for ${entry.path}:`, err);
    });
  }
}

// Offline edits to `path` not yet saved, if any (read while the score loads).
export function pendingAnnotations(path) {
  return getPending(path).catch(() => undefined);
}

// The annotation state to show, from what the server (or its cached copy)
// returned and `pending` (pendingAnnotations): offline edits win.
export function annotationState(annotData, pending) {
  if (pending) {
    return { pages: pending.pages, rotations: pending.rotations, etag: pending.baseEtag,
             base: pending.base, pending: true };
  }
  const base = { pages: annotData.pages || {}, rotations: annotData.rotations || {} };
  // "" is the server's etag for "no annotations yet"; null means unknown
  // (loaded offline with nothing cached).
  return { ...base, etag: annotData.etag ?? null, base: structuredClone(base), pending: false };
}

// The server's annotations replaced the open score's (conflict "Reload"):
// they're the new base, and any offline edits are dropped.
export function annotationsReloaded(s, data) {
  s.annotations = data.pages || {};
  s.rotations = data.rotations || {};
  markSaved(s, data.etag ?? null, currentState(s));
  s.annotationsOffline = false;
  return deletePending(s.currentScore.filepath);
}

// ---------------------------------------------------------------------------
// Pointer events
// ---------------------------------------------------------------------------

function canvasCoords(e, annotCanvas) {
  const rect = annotCanvas.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

function onPointerDown(e, annotCanvas, layoutIndex) {
  const s = getState();

  if (s.activeTool === "nav") {
    if (s.displayMode === "wide") return; // wide mode scrolls; use buttons to navigate
    if (s.displayMode === "2up" && s.pageLayouts.length === 2) {
      if (layoutIndex === 0) prevPage();
      else nextPage();
    } else {
      const { x } = canvasCoords(e, annotCanvas);
      const layout = s.pageLayouts[layoutIndex];
      if (layout) {
        if (x > layout.cssW / 2) nextPage();
        else prevPage();
      }
    }
    return;
  }
  // Pencil-only mode: pen tool ignores anything that isn't an Apple Pencil
  // (pointerType !== "pen"). Other tools are unaffected so the user can still
  // erase/text/navigate with finger or mouse. preventDefault is still called
  // so a stray palm touch on iPad doesn't fall through to Safari's native
  // selection / long-press callout (which manifests as "the whole page got
  // selected" while drawing with the pencil).
  if (s.activeTool === "pen" && s.pencilOnly && e.pointerType !== "pen") {
    e.preventDefault();
    return;
  }

  e.preventDefault();

  const layout = s.pageLayouts[layoutIndex];
  if (!layout) return;

  if (s.activeTool === "pen") {
    const { x, y } = canvasCoords(e, annotCanvas);
    s.currentStroke = [{ x, y }];
    s.strokeLayoutIndex = layoutIndex;
    annotCanvas.setPointerCapture(e.pointerId);
  } else if (s.activeTool === "eraser") {
    eraseAt(e, annotCanvas, layoutIndex);
    annotCanvas.setPointerCapture(e.pointerId);
  } else if (s.activeTool === "move") {
    if (startMove(e, annotCanvas, layoutIndex)) {
      annotCanvas.setPointerCapture(e.pointerId);
    }
  } else if (s.activeTool === "text") {
    handleTextClick(e, annotCanvas, layoutIndex);
  } else if (isPlacementTool(s.activeTool)) {
    // One mark per arming, then back to navigation.
    if (PLACEMENT_TOOLS[s.activeTool].place(s, pagePoint(e, annotCanvas, layout))) {
      saveAnnotations();
      setTool("nav");
      drawAnnotations();
    }
  }
}

function onPointerMove(e, annotCanvas, layoutIndex) {
  const s = getState();
  if (s.activeTool === "pen" && s.currentStroke.length > 0) {
    e.preventDefault();
    const { x, y } = canvasCoords(e, annotCanvas);
    s.currentStroke.push({ x, y });
    // Redraw now rather than next frame: browsers already deliver
    // pointermove once per frame, and a frame's lag shows under a Pencil.
    const layout = s.pageLayouts[layoutIndex];
    if (layout) drawPageAnnotations(annotCanvas, layout, layoutIndex);
  } else if (s.activeTool === "eraser" && e.buttons > 0) {
    e.preventDefault();
    eraseAt(e, annotCanvas, layoutIndex);
  } else if (s.activeTool === "move" && s.draggingAnnot) {
    e.preventDefault();
    moveTo(e, annotCanvas);
  }
}

function onPointerUp(e, annotCanvas, layoutIndex) {
  const s = getState();
  if (s.activeTool === "pen" && s.currentStroke.length > 1) {
    const layout = s.pageLayouts[layoutIndex];
    if (!layout) { s.currentStroke = []; return; }

    const pg = String(layout.page - 1);
    const rot = (s.rotations[pg] || 0) % 360;

    const norm = s.currentStroke.map(({ x, y }) => {
      const nx = x / layout.cssW;
      const ny = y / layout.cssH;
      return inverseTransformPt(nx, ny, rot);
    });

    pushUndo(pg);
    if (!s.annotations[pg]) s.annotations[pg] = [];
    s.annotations[pg].push(inkAnnotation(norm, s.penColor, s.penStyle, layout));
    saveAnnotations();
    drawAnnotations();
  } else if (s.activeTool === "move" && s.draggingAnnot) {
    endMove();
  } else if (s.activeTool === "eraser") {
    flushEraserSave();
  }
  s.currentStroke = [];
}

// The browser took the pointer away mid-gesture (e.g. iOS palm rejection or
// a system gesture) instead of it lifting. A half-drawn pen stroke is
// dropped rather than committed, and its preview wiped; a moved mark stays
// where it was dropped and marks the eraser already removed are saved, as
// at pointerup.
function onPointerCancel() {
  const s = getState();
  if (s.currentStroke.length > 0) {
    s.currentStroke = [];
    drawAnnotations();
  }
  if (s.activeTool === "move" && s.draggingAnnot) endMove();
  else if (s.activeTool === "eraser") flushEraserSave();
}

// ---------------------------------------------------------------------------
// Eraser
// ---------------------------------------------------------------------------

// An eraser drag can remove many marks; save them in one go when the
// gesture ends (pointerup or pointercancel) rather than once per mark.
let _eraserUnsaved = false;

function flushEraserSave() {
  if (!_eraserUnsaved) return;
  _eraserUnsaved = false;
  saveAnnotations();
}

function eraseAt(e, annotCanvas, layoutIndex) {
  const s = getState();
  const layout = s.pageLayouts[layoutIndex];
  if (!layout) return;

  const p = pagePoint(e, annotCanvas, layout);
  const hit = findTopHit(p, layout, 20);
  if (!hit) return;
  pushUndo(p.pg);
  const pageAnnots = s.annotations[p.pg];
  pageAnnots.splice(pageAnnots.indexOf(hit), 1);
  _eraserUnsaved = true;  // saved once when the gesture ends
  scheduleDraw();
}

// Hit tests: is CSS point (px, py) on the annotation, within `halo` px?

function hitInk(annot, px, py, w, h, rot, halo) {
  for (const pt of annot.points) {
    const [cx, cy] = transformPt(pt[0], pt[1], w, h, rot);
    if (Math.abs(cx - px) < halo && Math.abs(cy - py) < halo) return true;
  }
  return false;
}

function hitText(annot, px, py, w, h, rot, halo, pdfW) {
  const { cx, cy, sz, lines, lineH } = textLayout(annot, w, h, rot, pdfW);
  const longest = lines.reduce((m, l) => Math.max(m, l.length), 1);
  const textW = Math.max(sz, longest * sz * 0.6);
  // drawText puts the first line's bottom at the anchor and each further
  // line one lineH lower, so the box runs from a line height above the
  // anchor down to the last line's bottom.
  const top = cy - lineH;
  const bottom = cy + (lines.length - 1) * lineH;
  return px >= cx - halo && px <= cx + textW + halo &&
         py >= top - halo && py <= bottom + halo;
}

function hitStamp(annot, px, py, w, h, rot, halo, pdfW) {
  const [cx, cy] = transformPt(annot.x, annot.y, w, h, rot);
  const { wCss, hCss } = stampCssSize(annot.id, annot.size, w, pdfW);
  const halfW = Math.max(halo, wCss / 2);
  const halfH = Math.max(halo, hCss / 2);
  return Math.abs(cx - px) < halfW && Math.abs(cy - py) < halfH;
}

function hitStartStamp(annot, px, py, w, h, rot, halo, pdfW) {
  const [cx, cy] = transformPt(annot.x, annot.y, w, h, rot);
  const half = Math.max(halo, startStampCssSize(w, pdfW) / 2);
  return Math.abs(cx - px) < half && Math.abs(cy - py) < half;
}

// Per annotation type: draw(ctx, annot, w, h, rot, pdfW) and
// hit(annot, px, py, w, h, rot, halo, pdfW). Unknown types are skipped.
const ANNOT_TYPES = {
  ink: { draw: drawInk, hit: hitInk },
  text: { draw: drawText, hit: hitText },
  stamp: { draw: drawStamp, hit: hitStamp },
  startpage: { draw: drawStartStamp, hit: hitStartStamp },
};

// The pointer's position on `layout`'s page: CSS px on its canvas, the page
// key and rotation, and (nx, ny) in stored (unrotated, normalised) space.
function pagePoint(e, annotCanvas, layout) {
  const { x, y } = canvasCoords(e, annotCanvas);
  const pg = String(layout.page - 1);
  const rot = (getState().rotations[pg] || 0) % 360;
  const [nx, ny] = inverseTransformPt(x / layout.cssW, y / layout.cssH, rot);
  return { x, y, pg, rot, nx, ny };
}

// The topmost annotation on the page under point `p` (from pagePoint),
// optionally only of type `onlyType`, or null.
function findTopHit(p, layout, halo, onlyType = null) {
  const pageAnnots = getState().annotations[p.pg] || [];
  for (let i = pageAnnots.length - 1; i >= 0; i--) {
    const a = pageAnnots[i];
    const type = ANNOT_TYPES[a.type];
    if (type && (!onlyType || a.type === onlyType) &&
        type.hit(a, p.x, p.y, layout.cssW, layout.cssH, p.rot, halo, layout.pdfW)) {
      return a;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Move tool — drag a single annotation within its page
// ---------------------------------------------------------------------------

// Returns true if an annotation was grabbed (so the caller captures the
// pointer); false if the press landed on empty space.
function startMove(e, annotCanvas, layoutIndex) {
  const s = getState();
  const layout = s.pageLayouts[layoutIndex];
  if (!layout) return false;

  const p = pagePoint(e, annotCanvas, layout);
  const target = findTopHit(p, layout, 20);
  if (!target) return false;

  // Grab point and the annotation's geometry, both in stored page space,
  // so deltas survive rotation (the rotation offsets cancel in subtraction).
  const { pg, rot, nx: grabX, ny: grabY } = p;
  const orig = target.type === "ink"
    ? target.points.map(([px, py]) => [px, py])
    : { x: target.x, y: target.y };

  s.draggingAnnot = {
    pg,
    uuid: target.uuid,
    grabX,
    grabY,
    rot,
    cssW: layout.cssW,
    cssH: layout.cssH,
    orig,
    moved: false,
  };
  return true;
}

function moveTo(e, annotCanvas) {
  const s = getState();
  const d = s.draggingAnnot;
  if (!d) return;

  const pageAnnots = s.annotations[d.pg] || [];
  const annot = pageAnnots.find((a) => a.uuid === d.uuid);
  if (!annot) return;

  const { x, y } = canvasCoords(e, annotCanvas);
  const [curX, curY] = inverseTransformPt(x / d.cssW, y / d.cssH, d.rot);
  const dx = curX - d.grabX;
  const dy = curY - d.grabY;

  // Snapshot for undo only once an actual drag begins, so a stray tap on
  // an annotation doesn't pollute the undo stack.
  if (!d.moved) {
    pushUndo(d.pg);
    d.moved = true;
  }

  if (annot.type === "ink") {
    annot.points = d.orig.map(([ox, oy]) => [ox + dx, oy + dy]);
  } else {
    // text, stamp and startpage are all anchored by a single (x, y) point
    annot.x = d.orig.x + dx;
    annot.y = d.orig.y + dy;
  }
  scheduleDraw();
}

function endMove() {
  const s = getState();
  const d = s.draggingAnnot;
  s.draggingAnnot = null;
  if (d && d.moved) saveAnnotations();
  drawAnnotations();
}

// ---------------------------------------------------------------------------
// Text tool
// ---------------------------------------------------------------------------

function handleTextClick(e, annotCanvas, layoutIndex) {
  const s = getState();
  const layout = s.pageLayouts[layoutIndex];
  if (!layout) return;

  const p = pagePoint(e, annotCanvas, layout);
  const editAnnot = findTopHit(p, layout, 10, "text");

  if (editAnnot) {
    s.pendingTextAnnot = { pg: p.pg, editUuid: editAnnot.uuid };
  } else {
    s.pendingTextAnnot = { pg: p.pg, nx: p.nx, ny: p.ny, editUuid: null };
  }

  showTextDialog(editAnnot);
}

// Called by dialog-handlers when the text dialog closes with a result.
// `size` is a slider index from the text dialog's own size control, which
// has a wider range than the stamp size slider.
export function commitTextAnnotation(text, font, size) {
  const s = getState();
  if (!s.pendingTextAnnot) return;

  const { pg, nx, ny, editUuid } = s.pendingTextAnnot;
  s.pendingTextAnnot = null;

  pushUndo(pg);

  if (editUuid) {
    const pageAnnots = s.annotations[pg] || [];
    const existing = pageAnnots.find((a) => a.uuid === editUuid);
    if (existing) {
      existing.text = text;
      existing.font = font;
      existing.size = size;
    }
  } else {
    if (!s.annotations[pg]) s.annotations[pg] = [];
    s.annotations[pg].push({
      uuid: crypto.randomUUID(),
      type: "text",
      x: nx,
      y: ny,
      text,
      font,
      color: s.penColor,
      size,
    });
  }

  saveAnnotations();
  drawAnnotations();
}

export function cancelTextAnnotation() {
  getState().pendingTextAnnot = null;
}

// ---------------------------------------------------------------------------
// Placement: SMuFL stamp
// ---------------------------------------------------------------------------

function placeStamp(s, { pg, nx, ny }) {
  if (!s.selectedStamp) return false;
  pushUndo(pg);
  if (!s.annotations[pg]) s.annotations[pg] = [];
  s.annotations[pg].push({
    uuid: crypto.randomUUID(),
    type: "stamp",
    id: s.selectedStamp,
    x: nx,
    y: ny,
    size: parseInt(sizeSlider.value, 10),
    color: s.penColor,
  });
  return true;
}

// ---------------------------------------------------------------------------
// Placement: start-page stamp — one per document; placing it again moves it
// ---------------------------------------------------------------------------

function placeStartPage(s, { pg, nx, ny }) {
  // Remove the existing stamp wherever it is. Undo is per page, so a move
  // across pages is undone on each page separately.
  for (const [p, annots] of Object.entries(s.annotations)) {
    if (p !== pg && annots.some((a) => a.type === "startpage")) {
      pushUndo(p);
      s.annotations[p] = annots.filter((a) => a.type !== "startpage");
    }
  }
  pushUndo(pg);
  s.annotations[pg] = (s.annotations[pg] || []).filter((a) => a.type !== "startpage");
  s.annotations[pg].push({
    uuid: crypto.randomUUID(),
    type: "startpage",
    x: nx,
    y: ny,
  });
  return true;
}

// ---------------------------------------------------------------------------
// Init event listeners
// ---------------------------------------------------------------------------

function setupAnnotCanvas(annotCanvas, layoutIndex) {
  annotCanvas.addEventListener("pointerdown", (e) => onPointerDown(e, annotCanvas, layoutIndex));
  annotCanvas.addEventListener("pointermove", (e) => onPointerMove(e, annotCanvas, layoutIndex));
  annotCanvas.addEventListener("pointerup", (e) => onPointerUp(e, annotCanvas, layoutIndex));
  annotCanvas.addEventListener("pointercancel", onPointerCancel);
}

export function initAnnotationEvents() {
  setupAnnotCanvas(annotCanvas1, 0);
  setupAnnotCanvas(annotCanvas2, 1);

  // Clicking off a page (the container padding, not a page canvas) cancels
  // stamp mode. A click on a canvas places the stamp and switches back to nav
  // first (it bubbles here afterwards), so this only fires for off-page clicks.
  pdfContainer.addEventListener("pointerdown", (e) => {
    if (isPlacementTool(getState().activeTool) && !e.target.closest(".annot-layer")) {
      setTool("nav");
    }
  });

  btnNav.addEventListener("click", () => setTool("nav"));
  btnPen.addEventListener("click", () => setTool("pen"));
  btnText.addEventListener("click", () => setTool("text"));
  btnEraser.addEventListener("click", () => setTool("eraser"));
  btnMove.addEventListener("click", () => setTool("move"));
  btnStartPage.addEventListener("click", () => {
    setTool(getState().activeTool === "startpage" ? "nav" : "startpage");
  });

  // Pencil-only toggle — restored from localStorage so iPad users don't
  // re-enable on every reload.
  setPencilOnly(loadPencilOnlyPref());
  btnPencilOnly.addEventListener("click", () => {
    setPencilOnly(!getState().pencilOnly);
  });

  document.querySelectorAll(".swatch").forEach((sw) => {
    sw.addEventListener("click", () => {
      document.querySelectorAll(".swatch").forEach((s) => s.classList.remove("selected"));
      sw.classList.add("selected");
      getState().penColor = sw.dataset.color;
      updatePenChip();
    });
  });

  // The selected swatch is the source of truth for the default pen colour, so
  // it can't drift from the markup. Sync state from it on init.
  const selectedSwatch = document.querySelector(".swatch.selected");
  if (selectedSwatch) getState().penColor = selectedSwatch.dataset.color;

  // Pen style: the chip shows the current stroke and opens the grid; picking
  // a cell sets width and transparency together and arms the pen.
  getState().penStyle = loadPenStylePref();
  updatePenChip();
  btnPenStyle.addEventListener("click", () => {
    renderPenGrid();
    penDialog.showModal();
  });
  penGrid.addEventListener("click", (e) => {
    const cell = e.target.closest(".pen-cell");
    if (!cell) return;
    setPenStyle(parseInt(cell.dataset.row, 10), parseInt(cell.dataset.col, 10));
    penDialog.close();
    setTool("pen");
  });
  penCancel.addEventListener("click", () => penDialog.close());

  btnUndo.addEventListener("click", () => doUndo());
  btnRotCW.addEventListener("click", () => rotatePage(90));
  btnRotCCW.addEventListener("click", () => rotatePage(-90));
}
