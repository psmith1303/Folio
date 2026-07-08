// ---------------------------------------------------------------------------
// Shared utilities
// ---------------------------------------------------------------------------

export const UNDO_DEPTH = 20;

// The size slider maps to these PDF point sizes, shared by text and stamps
// (the pen/stamp toolbar slider only goes up to index 8 / 22pt; the text
// dialog's own slider uses the full range up to 44pt). Kept in sync with
// _POINT_SIZES in web/core.py.
export const POINT_SIZES = [9, 10, 11, 12, 14, 16, 18, 22, 26, 30, 34, 38, 44];

export function sizeToPt(size) {
  const i = Math.max(1, Math.min(POINT_SIZES.length, size || 1)) - 1;
  return POINT_SIZES[i];
}

// Glyphs that render tiny in fall-back fonts and need ~6x scaling to be
// readable as music notation. Dynamics ("p", "f", ...) are deliberately
// NOT in this set \u2014 they're just letters that should follow the size
// slider, otherwise typing "p" or "f" as plain text annotations comes
// out enormous regardless of the size setting.
export const NOTE_GLYPHS = new Set([
  "\u{1D15E}", "\u2669", "\u2669.", "\u266A",
  "\u266D", "\u266F", "\u266E",
]);

export function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// Forward transform: normalized page coords -> display coords
// Annotation coords are stored in original (unrotated) page space.
export function transformPt(nx, ny, w, h, rot) {
  if (rot === 90)  { [nx, ny] = [ny, 1.0 - nx]; }
  else if (rot === 180) { [nx, ny] = [1.0 - nx, 1.0 - ny]; }
  else if (rot === 270) { [nx, ny] = [1.0 - ny, nx]; }
  return [nx * w, ny * h];
}

// Inverse transform: display coords (normalized) -> original page coords
// Undoes the display rotation to recover storage coords.
export function inverseTransformPt(nx, ny, rot) {
  if (rot === 90)  { return [1.0 - ny, nx]; }
  if (rot === 180) { return [1.0 - nx, 1.0 - ny]; }
  if (rot === 270) { return [ny, 1.0 - nx]; }
  return [nx, ny];
}
