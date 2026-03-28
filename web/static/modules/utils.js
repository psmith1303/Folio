// ---------------------------------------------------------------------------
// Shared utilities
// ---------------------------------------------------------------------------

export const UNDO_DEPTH = 20;

export const MUSICAL_SYMBOLS = new Set([
  "\u{1D15E}", "\u2669", "\u2669.", "\u266A",
  "pp", "p", "mp", "mf", "f", "ff",
  "sfz", "cresc", "dim",
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
