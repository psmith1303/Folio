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

const STAMPS_BASE = "/stamps";
const PER_PAGE = 36;

let _manifest = [];               // [{ id, label, file }]
const _svgText = new Map();       // id -> raw SVG template (uses currentColor)
const _imgCache = new Map();      // "id|color" -> HTMLImageElement
let _page = 0;

let _selectHandler = null;
export function setStampSelectHandler(fn) { _selectHandler = fn; }

let _readyHandler = null;
export function setStampsReadyHandler(fn) { _readyHandler = fn; }

export function getStamps() { return _manifest; }

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

// Build a data URL for a stamp at a given colour and pixel size. currentColor
// is replaced with the actual colour, and explicit width/height + xmlns are
// injected so the SVG renders correctly inside an <img>/cursor.
function stampDataUrl(id, color, px) {
  let svg = _svgText.get(id);
  if (!svg) return null;
  svg = svg.replaceAll("currentColor", color);
  if (!/\bwidth=/.test(svg)) {
    svg = svg.replace("<svg ", `<svg width="${px}" height="${px}" `);
  }
  return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}

// Returns a cached HTMLImageElement for (id, color), rasterised at a fixed
// large size so the canvas can downscale crisply. Returns null if not ready;
// onReady fires once the image finishes loading so the caller can redraw.
export function getStampImage(id, color, onReady) {
  const key = `${id}|${color}`;
  let img = _imgCache.get(key);
  if (img) {
    if (img.complete && img.naturalWidth > 0) return img;
    if (onReady) img.addEventListener("load", onReady, { once: true });
    return null;
  }
  const url = stampDataUrl(id, color, 200);
  if (!url) return null;
  img = new Image();
  if (onReady) img.addEventListener("load", onReady, { once: true });
  img.src = url;
  _imgCache.set(key, img);
  return (img.complete && img.naturalWidth > 0) ? img : null;
}

export function stampCursorDataUrl(id, color, px) {
  return stampDataUrl(id, color, px);
}

// ---------------------------------------------------------------------------
// Palette
// ---------------------------------------------------------------------------

function renderPalette(grid, pageInfo, prevBtn, nextBtn) {
  const total = _manifest.length;
  const pages = Math.max(1, Math.ceil(total / PER_PAGE));
  if (_page >= pages) _page = pages - 1;

  grid.style.color = getState().penColor || "black";
  grid.innerHTML = "";
  const start = _page * PER_PAGE;
  for (const st of _manifest.slice(start, start + PER_PAGE)) {
    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = "stamp-tile";
    tile.title = st.label;
    tile.innerHTML = _svgText.get(st.id) || "";
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
