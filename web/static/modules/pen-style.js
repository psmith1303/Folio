// ---------------------------------------------------------------------------
// Pen style: a grid of widths (rows) x transparencies (columns)
// ---------------------------------------------------------------------------
//
// The toolbar chip shows the current stroke and opens the grid; one tap on a
// cell sets width and transparency together. This module owns the grid, the
// chip and the saved choice; annotations.js draws and commits strokes in the
// style penStyleAt gives.

import { getState } from "./state.js";
import { btnPenStyle, penDialog, penGrid, penCancel } from "./dom.js";
import { cssPerPt, readPref, writePref } from "./utils.js";

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

export function penStyleAt(row, col) {
  const k = col === HIGHLIGHT_COL ? HIGHLIGHT_WIDTH_FACTOR : 1;
  return { widthPt: PEN_WIDTHS_PT[row] * k, opacity: PEN_OPACITIES[col] };
}

function isPenStyle(v) {
  return !!v && Number.isInteger(v.row) && Number.isInteger(v.col)
    && v.row >= 0 && v.row < PEN_WIDTHS_PT.length
    && v.col >= 0 && v.col < PEN_OPACITIES.length;
}

function loadPenStylePref() {
  try {
    const v = JSON.parse(readPref(PEN_STYLE_STORAGE_KEY));
    if (isPenStyle(v)) return { row: v.row, col: v.col };
  } catch (_) {
    // A garbled value: use the default.
  }
  return { ...DEFAULT_PEN_STYLE };
}

function setPenStyle(row, col) {
  getState().penStyle = { row, col };
  writePref(PEN_STYLE_STORAGE_KEY, JSON.stringify({ row, col }));
  updatePenChip();
}

function penSampleSvg(color, widthPx, opacity, len, height) {
  const pad = widthPx / 2 + 2;
  return `<svg width="${len}" height="${height}" aria-hidden="true">`
    + `<line x1="${pad}" y1="${height / 2}" x2="${len - pad}" y2="${height / 2}" `
    + `stroke="${color}" stroke-width="${widthPx}" stroke-opacity="${opacity}" `
    + `stroke-linecap="round"/></svg>`;
}

// A sample of cell (row, col) in the current colour, at its on-page width
// on the page as currently shown, capped at maxPx.
function penSample(s, row, col, maxPx, len, height) {
  const { widthPt, opacity } = penStyleAt(row, col);
  const layout = s.pageLayouts[0];
  const px = Math.max(1, Math.min(maxPx, widthPt * cssPerPt(layout?.cssW, layout?.pdfW)));
  return penSampleSvg(s.penColor, px, opacity, len, height);
}

export function updatePenChip() {
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

// Restores the saved style and wires the chip and grid. `onPick` runs after
// a cell is picked (annotations.js arms the pen).
export function initPenStyle(onPick) {
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
    onPick();
  });
  penCancel.addEventListener("click", () => penDialog.close());
}
