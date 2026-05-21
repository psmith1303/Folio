// ---------------------------------------------------------------------------
// Annotation drawing, tools, pointer events, save/load, undo
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  annotCanvas1, annotCanvas2, sizeSlider,
  btnNav, btnPen, btnText, btnEraser, btnMove, btnPencilOnly, btnUndo,
  btnRotCCW, btnRotCW,
} from "./dom.js";
import { api } from "./api.js";
import {
  NOTE_GLYPHS, UNDO_DEPTH, transformPt, inverseTransformPt, esc,
} from "./utils.js";

// Callbacks registered by dialog-handlers to avoid circular deps
let _conflictHandler = null;
let _textDialogHandler = null;

export function setConflictHandler(fn) { _conflictHandler = fn; }
export function setTextDialogHandler(fn) { _textDialogHandler = fn; }

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

export function drawAnnotations() {
  const s = getState();
  for (let i = 0; i < s.pageLayouts.length; i++) {
    const layout = s.pageLayouts[i];
    const ac = i === 0 ? annotCanvas1 : annotCanvas2;
    drawPageAnnotations(ac, layout);
  }
}

function drawPageAnnotations(annotCanvas, layout) {
  const s = getState();
  const dpr = window.devicePixelRatio || 1;
  const ctx = annotCanvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, layout.cssW, layout.cssH);

  const pg = String(layout.page - 1);
  const pageAnnots = s.annotations[pg] || [];
  const rot = (s.rotations[pg] || 0) % 360;

  for (const annot of pageAnnots) {
    if (annot.type === "ink") {
      drawInk(ctx, annot, layout.cssW, layout.cssH, rot);
    } else if (annot.type === "text") {
      drawText(ctx, annot, layout.cssW, layout.cssH, rot);
    }
  }
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
  if (NOTE_GLYPHS.has(annot.text)) {
    sz = Math.round(sz * 6);
  }
  const font = annot.font || "sans-serif";
  ctx.font = `${sz}px ${font}`;
  ctx.fillStyle = annot.color || "black";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const lines = String(annot.text).split("\n");
  const lineH = sz * 1.2;
  const startY = cy - ((lines.length - 1) * lineH) / 2;
  for (let i = 0; i < lines.length; i++) {
    ctx.fillText(lines[i], cx, startY + i * lineH);
  }
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

export function setTool(tool) {
  const s = getState();
  s.activeTool = tool;
  document.querySelectorAll(".tool-btn").forEach((b) => b.classList.remove("active"));
  const map = { nav: btnNav, pen: btnPen, text: btnText, eraser: btnEraser, move: btnMove };
  if (map[tool]) map[tool].classList.add("active");

  for (const ac of [annotCanvas1, annotCanvas2]) {
    ac.classList.remove("tool-pen", "tool-text", "tool-eraser", "tool-move");
    if (tool !== "nav") ac.classList.add(`tool-${tool}`);
    ac.style.touchAction = tool === "nav" ? "auto" : "none";
  }
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
// Page rotation
// ---------------------------------------------------------------------------

let _renderPage = null;
export function setRenderPageFn(fn) { _renderPage = fn; }

let _invalidatePrerender = null;
export function setInvalidatePrerenderFn(fn) { _invalidatePrerender = fn; }

export function rotatePage(delta) {
  const s = getState();
  if (!s.pdfDoc) return;
  const pg = String(s.currentPage - 1);
  const current = (s.rotations[pg] || 0) % 360;
  const next = (current + delta + 360) % 360;
  s.rotations[pg] = next;
  saveAnnotations();
  if (_invalidatePrerender) _invalidatePrerender(s.currentPage);
  if (_renderPage) _renderPage();
}

// ---------------------------------------------------------------------------
// Save annotations
// ---------------------------------------------------------------------------

// Serialize saves so each one waits for the previous to complete,
// preventing false etag conflicts from concurrent in-flight requests.
let _saveChain = Promise.resolve();

export function saveAnnotations(force = false) {
  const filepath = getState().currentScore?.filepath;
  _saveChain = _saveChain.then(() => {
    const s = getState();
    if (!s.currentScore || s.currentScore.filepath !== filepath) return;
    return _doSaveAnnotations(s, force);
  }).catch((err) => {
    console.error("Save chain error:", err);
  });
}

async function _doSaveAnnotations(s, force) {
  try {
    const payload = {
      path: s.currentScore.filepath,
      pages: s.annotations,
      rotations: s.rotations,
    };
    if (!force && s.annotationEtag !== null) {
      payload.expected_etag = s.annotationEtag;
    }
    const result = await api("/api/annotations", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (result.etag) {
      s.annotationEtag = result.etag;
    }
  } catch (err) {
    if (err.message && err.message.includes("409")) {
      if (_conflictHandler) _conflictHandler();
      return;
    }
    console.error("Failed to save annotations:", err);
  }
}

// ---------------------------------------------------------------------------
// Pointer events
// ---------------------------------------------------------------------------

function canvasCoords(e, annotCanvas) {
  const rect = annotCanvas.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

// Navigation callbacks — set by viewer module to avoid circular dep
let _nextPage = null;
let _prevPage = null;
export function setNavCallbacks(next, prev) {
  _nextPage = next;
  _prevPage = prev;
}

function onPointerDown(e, annotCanvas, layoutIndex) {
  const s = getState();

  if (s.activeTool === "nav") {
    if (s.displayMode === "wide") return; // wide mode scrolls; use buttons to navigate
    if (s.displayMode === "2up" && s.pageLayouts.length === 2) {
      if (layoutIndex === 0) _prevPage();
      else _nextPage();
    } else {
      const { x } = canvasCoords(e, annotCanvas);
      const layout = s.pageLayouts[layoutIndex];
      if (layout) {
        if (x > layout.cssW / 2) _nextPage();
        else _prevPage();
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
  }
}

function onPointerMove(e, annotCanvas, layoutIndex) {
  const s = getState();
  if (s.activeTool === "pen" && s.currentStroke.length > 0) {
    e.preventDefault();
    const { x, y } = canvasCoords(e, annotCanvas);
    s.currentStroke.push({ x, y });

    const dpr = window.devicePixelRatio || 1;
    const ctx = annotCanvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const prev = s.currentStroke[s.currentStroke.length - 2];
    ctx.beginPath();
    ctx.moveTo(prev.x, prev.y);
    ctx.lineTo(x, y);
    ctx.strokeStyle = s.penColor;
    ctx.lineWidth = parseInt(sizeSlider.value, 10);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.stroke();
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
    s.annotations[pg].push({
      uuid: crypto.randomUUID(),
      type: "ink",
      points: norm,
      color: s.penColor,
      width: parseInt(sizeSlider.value, 10),
    });
    saveAnnotations();
    drawAnnotations();
  } else if (s.activeTool === "move" && s.draggingAnnot) {
    endMove();
  }
  s.currentStroke = [];
}

// ---------------------------------------------------------------------------
// Eraser
// ---------------------------------------------------------------------------

function eraseAt(e, annotCanvas, layoutIndex) {
  const s = getState();
  const layout = s.pageLayouts[layoutIndex];
  if (!layout) return;

  const { x, y } = canvasCoords(e, annotCanvas);
  const pg = String(layout.page - 1);
  const pageAnnots = s.annotations[pg];
  if (!pageAnnots || pageAnnots.length === 0) return;

  const rot = (s.rotations[pg] || 0) % 360;
  const halo = 20;

  for (let i = pageAnnots.length - 1; i >= 0; i--) {
    if (hitTest(pageAnnots[i], x, y, layout.cssW, layout.cssH, rot, halo)) {
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
    if (NOTE_GLYPHS.has(annot.text)) sz = Math.round(sz * 6);
    const lines = String(annot.text).split("\n");
    const halfW = Math.max(halo, sz);
    const halfH = Math.max(halo, (lines.length * sz * 1.2) / 2);
    return Math.abs(cx - px) < halfW && Math.abs(cy - py) < halfH;
  }
  return false;
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

  const pg = String(layout.page - 1);
  const pageAnnots = s.annotations[pg];
  if (!pageAnnots || pageAnnots.length === 0) return false;

  const { x, y } = canvasCoords(e, annotCanvas);
  const rot = (s.rotations[pg] || 0) % 360;
  const halo = 20;

  let target = null;
  for (let i = pageAnnots.length - 1; i >= 0; i--) {
    if (hitTest(pageAnnots[i], x, y, layout.cssW, layout.cssH, rot, halo)) {
      target = pageAnnots[i];
      break;
    }
  }
  if (!target) return false;

  // Grab point and the annotation's geometry, both in stored page space,
  // so deltas survive rotation (the rotation offsets cancel in subtraction).
  const [grabX, grabY] = inverseTransformPt(
    x / layout.cssW, y / layout.cssH, rot,
  );
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
  } else if (annot.type === "text") {
    annot.x = d.orig.x + dx;
    annot.y = d.orig.y + dy;
  }
  drawAnnotations();
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

  const { x, y } = canvasCoords(e, annotCanvas);
  const pg = String(layout.page - 1);
  const rot = (s.rotations[pg] || 0) % 360;

  const pageAnnots = s.annotations[pg] || [];
  let editAnnot = null;
  for (let i = pageAnnots.length - 1; i >= 0; i--) {
    const a = pageAnnots[i];
    if (a.type === "text" && hitTest(a, x, y, layout.cssW, layout.cssH, rot, 10)) {
      editAnnot = a;
      break;
    }
  }

  if (editAnnot) {
    s.pendingTextAnnot = { pg, editUuid: editAnnot.uuid };
  } else {
    const nx = x / layout.cssW;
    const ny = y / layout.cssH;
    const [origX, origY] = inverseTransformPt(nx, ny, rot);
    s.pendingTextAnnot = { pg, nx: origX, ny: origY, editUuid: null };
  }

  if (_textDialogHandler) _textDialogHandler(editAnnot);
}

// Called by dialog-handlers when the text dialog closes with a result
export function commitTextAnnotation(text, font) {
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
      existing.color = s.penColor;
      existing.size = parseInt(sizeSlider.value, 10);
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
      size: parseInt(sizeSlider.value, 10),
    });
  }

  saveAnnotations();
  drawAnnotations();
}

export function cancelTextAnnotation() {
  getState().pendingTextAnnot = null;
}

// ---------------------------------------------------------------------------
// Init event listeners
// ---------------------------------------------------------------------------

function setupAnnotCanvas(annotCanvas, layoutIndex) {
  annotCanvas.addEventListener("pointerdown", (e) => onPointerDown(e, annotCanvas, layoutIndex));
  annotCanvas.addEventListener("pointermove", (e) => onPointerMove(e, annotCanvas, layoutIndex));
  annotCanvas.addEventListener("pointerup", (e) => onPointerUp(e, annotCanvas, layoutIndex));
}

export function initAnnotationEvents() {
  setupAnnotCanvas(annotCanvas1, 0);
  setupAnnotCanvas(annotCanvas2, 1);

  btnNav.addEventListener("click", () => setTool("nav"));
  btnPen.addEventListener("click", () => setTool("pen"));
  btnText.addEventListener("click", () => setTool("text"));
  btnEraser.addEventListener("click", () => setTool("eraser"));
  btnMove.addEventListener("click", () => setTool("move"));

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
    });
  });

  btnUndo.addEventListener("click", () => doUndo());
  btnRotCW.addEventListener("click", () => rotatePage(90));
  btnRotCCW.addEventListener("click", () => rotatePage(-90));
}
