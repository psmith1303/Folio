// ---------------------------------------------------------------------------
// Annotation drawing, tools, pointer events, save/load, undo
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  annotCanvas1, annotCanvas2, sizeSlider,
  btnNav, btnPen, btnText, btnEraser, btnUndo, btnRotCCW, btnRotCW,
} from "./dom.js";
import { api } from "./api.js";
import {
  MUSICAL_SYMBOLS, UNDO_DEPTH, transformPt, inverseTransformPt, esc,
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
// Tools
// ---------------------------------------------------------------------------

export function setTool(tool) {
  const s = getState();
  s.activeTool = tool;
  document.querySelectorAll(".tool-btn").forEach((b) => b.classList.remove("active"));
  const map = { nav: btnNav, pen: btnPen, text: btnText, eraser: btnEraser };
  if (map[tool]) map[tool].classList.add("active");

  for (const ac of [annotCanvas1, annotCanvas2]) {
    ac.classList.remove("tool-pen", "tool-text", "tool-eraser");
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
// Page rotation
// ---------------------------------------------------------------------------

let _renderPage = null;
export function setRenderPageFn(fn) { _renderPage = fn; }

export function rotatePage(delta) {
  const s = getState();
  if (!s.pdfDoc) return;
  const pg = String(s.currentPage - 1);
  const current = (s.rotations[pg] || 0) % 360;
  const next = (current + delta + 360) % 360;
  s.rotations[pg] = next;
  saveAnnotations();
  if (_renderPage) _renderPage();
}

// ---------------------------------------------------------------------------
// Save annotations
// ---------------------------------------------------------------------------

export async function saveAnnotations(force = false) {
  const s = getState();
  if (!s.currentScore) return;
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
    if (s.displayMode === "wide") return;
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
    if (MUSICAL_SYMBOLS.has(annot.text)) sz = Math.round(sz * 6);
    const textHalo = Math.max(halo, sz);
    return Math.abs(cx - px) < textHalo && Math.abs(cy - py) < textHalo;
  }
  return false;
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
