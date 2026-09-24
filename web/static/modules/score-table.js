// ---------------------------------------------------------------------------
// Score tables — the Library, Recent and Newest lists share one row layout:
// composer, title, tags, an optional extra column, then the offline-cache
// button. Rows are built as one HTML string, and each table has a single
// click listener: the cache button toggles caching, anywhere else in a row
// opens its score.
// ---------------------------------------------------------------------------

import { esc } from "./utils.js";
import { CACHE_AVAILABLE, cacheButtonHtml, onCacheButtonClick } from "./cache.js";
import { openScore } from "./viewer.js";

// tbody -> Map(filepath -> the record openScore gets for that row)
const _toOpen = new WeakMap();

// Render `items` ({ filepath, composer, title, tags }) into `tbody`. `extra`
// returns an optional extra column's HTML for an item; `toOpen` the record to
// open for it (default: the item itself).
export function renderScoreRows(tbody, items, { extra = null, toOpen = (it) => it } = {}) {
  if (!_toOpen.has(tbody)) tbody.addEventListener("click", onRowClick);
  const byPath = new Map();
  let html = "";
  for (const it of items) {
    byPath.set(it.filepath, toOpen(it));
    const composer = esc(it.composer);
    const title = esc(it.title);
    const tags = esc((it.tags || []).join(", "));
    html += `<tr data-filepath="${esc(it.filepath)}">`
      + `<td title="${composer}">${composer}</td>`
      + `<td title="${title}">${title}</td>`
      + `<td title="${tags}">${tags}</td>`
      + (extra ? `<td>${extra(it)}</td>` : "")
      + (CACHE_AVAILABLE ? `<td class="cache-col">${cacheButtonHtml(it.filepath)}</td>` : "")
      + "</tr>";
  }
  _toOpen.set(tbody, byPath);
  tbody.innerHTML = html;
}

function onRowClick(e) {
  if (onCacheButtonClick(e)) return;
  const tr = e.target.closest("tr[data-filepath]");
  if (tr) openScore(_toOpen.get(e.currentTarget).get(tr.dataset.filepath));
}
