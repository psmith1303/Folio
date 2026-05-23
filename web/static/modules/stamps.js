// ---------------------------------------------------------------------------
// Stamps — preset SVG marks placed onto the score (dynamics, articulations…)
//
// Assets live in /stamps/ as currentColor SVGs plus a stamps.json manifest.
// This module owns the asset cache, the colour/size helpers used to render a
// stamp on the canvas and as a desktop cursor, and the paginated palette.
// It deliberately does NOT import annotations.js — selecting a stamp calls a
// handler registered by app.js, keeping the dependency one-directional.
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import { sizeToPt } from "./utils.js";

const STAMPS_BASE = "/stamps";
const PER_PAGE = 36;
const PALETTE_MAX_PX = 72;   // cap a preview dimension so huge stamps don't blow up the dialog

let _manifest = [];               // [{ id, label, file, w, h }]
const _svgText = new Map();       // id -> raw SVG template (uses currentColor)
const _meta = new Map();          // id -> { w, h } in staff spaces
const _imgCache = new Map();      // "id|color" -> HTMLImageElement
let _page = 0;

let _selectHandler = null;
export function setStampSelectHandler(fn) { _selectHandler = fn; }

let _readyHandler = null;
export function setStampsReadyHandler(fn) { _readyHandler = fn; }

export function getStamps() { return _manifest; }

// Per-stamp width/height in staff spaces (drives true SMuFL proportions).
export function getStampMeta(id) { return _meta.get(id) || null; }

// ---------------------------------------------------------------------------
// Asset loading
// ---------------------------------------------------------------------------

export async function loadStampAssets() {
  try {
    const resp = await fetch(`${STAMPS_BASE}/stamps.json`, { cache: "no-store" });
    if (!resp.ok) return;
    const data = await resp.json();
    _manifest = Array.isArray(data.stamps) ? data.stamps : [];
    await Promise.all(_manifest.map(async (st) => {
      _meta.set(st.id, { w: st.w || 1, h: st.h || 1 });
      try {
        const r = await fetch(`${STAMPS_BASE}/${st.file}`);
        if (r.ok) _svgText.set(st.id, await r.text());
      } catch { /* leave missing; getStampImage returns null */ }
    }));
    if (_readyHandler) _readyHandler();
  } catch (err) {
    console.warn("Failed to load stamps:", err);
  }
}

// Build a data URL for a stamp at a given colour, rasterised at hPx tall with
// width set to preserve the glyph's aspect ratio. currentColor is replaced
// with the actual colour; explicit width/height are injected so the SVG
// renders at a known size inside an <img>.
function stampDataUrl(id, color, hPx) {
  let svg = _svgText.get(id);
  if (!svg) return null;
  const meta = _meta.get(id);
  const aspect = meta && meta.h ? meta.w / meta.h : 1;
  const wPx = Math.max(1, Math.round(hPx * aspect));
  svg = svg.replaceAll("currentColor", color);
  svg = svg.replace("<svg ", `<svg width="${wPx}" height="${Math.round(hPx)}" `);
  return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}

// Returns a cached HTMLImageElement for (id, color), rasterised at a fixed
// reference height (aspect-correct width) so the canvas can scale it crisply.
// Returns null if not ready; onReady fires once the image loads so the caller
// can redraw.
export function getStampImage(id, color, onReady) {
  const key = `${id}|${color}`;
  let img = _imgCache.get(key);
  if (img) {
    if (img.complete && img.naturalWidth > 0) return img;
    if (onReady) img.addEventListener("load", onReady, { once: true });
    return null;
  }
  const url = stampDataUrl(id, color, 256);
  if (!url) return null;
  img = new Image();
  if (onReady) img.addEventListener("load", onReady, { once: true });
  img.src = url;
  _imgCache.set(key, img);
  return (img.complete && img.naturalWidth > 0) ? img : null;
}

// PNG data URL for the desktop cursor, rendered via canvas. PNG cursors are
// reliable across browsers; SVG-data-URI cursors are not (Firefox often
// ignores them and falls back to the keyword cursor). Returns null until the
// stamp image has loaded; onReady fires on load so the caller can re-apply.
export function stampCursorPng(id, color, wPx, hPx, onReady) {
  const img = getStampImage(id, color, onReady);
  if (!img) return null;
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(wPx));
  c.height = Math.max(1, Math.round(hPx));
  try {
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    return c.toDataURL("image/png");
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Palette
// ---------------------------------------------------------------------------

// CSS px per staff space at the current slider value and page render scale —
// the same conversion the canvas uses, so palette previews match the page.
function palettePxPerStaffSpace() {
  const slider = parseInt(
    (document.getElementById("size-slider") || {}).value, 10,
  ) || 1;
  const pt = sizeToPt(slider);
  const layout = getState().pageLayouts[0];
  const cssPerPt = layout && layout.pdfW ? layout.cssW / layout.pdfW : 1;
  return pt * cssPerPt;
}

function renderPalette(grid, pageInfo, prevBtn, nextBtn) {
  const total = _manifest.length;
  const pages = Math.max(1, Math.ceil(total / PER_PAGE));
  if (_page >= pages) _page = pages - 1;

  const pxPerSS = palettePxPerStaffSpace();
  grid.innerHTML = "";
  const start = _page * PER_PAGE;
  for (const st of _manifest.slice(start, start + PER_PAGE)) {
    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = "stamp-tile";
    tile.title = st.label;
    // Previews use the default foreground colour (currentColor) for contrast
    // against the dialog, regardless of the selected pen colour.
    tile.innerHTML = _svgText.get(st.id) || "";
    // Size the preview to the stamp's true on-page size (capped so a huge
    // stamp at a large slider value can't blow up the dialog).
    const svg = tile.querySelector("svg");
    if (svg) {
      let wPx = (st.w || 1) * pxPerSS;
      let hPx = (st.h || 1) * pxPerSS;
      const m = Math.max(wPx, hPx);
      if (m > PALETTE_MAX_PX) { const k = PALETTE_MAX_PX / m; wPx *= k; hPx *= k; }
      svg.setAttribute("width", Math.max(1, Math.round(wPx)));
      svg.setAttribute("height", Math.max(1, Math.round(hPx)));
    }
    tile.addEventListener("click", () => {
      const dialog = document.getElementById("stamp-dialog");
      if (dialog) dialog.close();
      if (_selectHandler) _selectHandler(st.id);
    });
    grid.appendChild(tile);
  }
  pageInfo.textContent = `${_page + 1} / ${pages}`;
  prevBtn.disabled = _page === 0;
  nextBtn.disabled = _page >= pages - 1;
}

export function initStampPalette() {
  const btnStamp = document.getElementById("btn-stamp");
  const dialog = document.getElementById("stamp-dialog");
  const grid = document.getElementById("stamp-grid");
  const pageInfo = document.getElementById("stamp-page-info");
  const prevBtn = document.getElementById("stamp-prev");
  const nextBtn = document.getElementById("stamp-next");
  const closeBtn = document.getElementById("stamp-close");
  if (!btnStamp || !dialog) return;

  btnStamp.addEventListener("click", () => {
    _page = 0;
    renderPalette(grid, pageInfo, prevBtn, nextBtn);
    dialog.showModal();
  });
  prevBtn.addEventListener("click", () => {
    if (_page > 0) { _page--; renderPalette(grid, pageInfo, prevBtn, nextBtn); }
  });
  nextBtn.addEventListener("click", () => {
    _page++; renderPalette(grid, pageInfo, prevBtn, nextBtn);
  });
  closeBtn.addEventListener("click", () => dialog.close());
}
