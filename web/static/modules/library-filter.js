// ---------------------------------------------------------------------------
// Library filtering — pure functions over the score list (no DOM, no state),
// shared by the library view and the setlist song picker.
//
// The rules are the ones the server's get_library() applied before
// /api/library became a plain list, and its outputs for a grid of cases are
// recorded in tests/data/library_filter_golden.json: case-insensitive
// substring on title or composer, exact composer match, and all selected tags
// present. Tag matching is an exact subset — selected tags always come from
// the library's own tag list, so no case folding is needed.
// ---------------------------------------------------------------------------

const cmpStr = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

// Element-wise, shorter-is-smaller — matches how Python orders tuples/lists.
function cmpArr(a, b) {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const c = cmpStr(a[i], b[i]);
    if (c !== 0) return c;
  }
  return a.length - b.length;
}

const SORTERS = {
  composer: (a, b) => cmpArr(
    [a.composer.toLowerCase(), a.title.toLowerCase()],
    [b.composer.toLowerCase(), b.title.toLowerCase()],
  ),
  title: (a, b) => cmpArr(
    [a.title.toLowerCase(), a.composer.toLowerCase()],
    [b.title.toLowerCase(), b.composer.toLowerCase()],
  ),
  // Title is a third key the old endpoint did not have. Sorting by tags
  // leaves same-composer/same-tag scores tied, and their order would then
  // depend on how the fetched list happened to be ordered. Breaking on title
  // makes the ordering total and independent of that.
  tags: (a, b) =>
    cmpArr([...a.tags].sort(cmpStr), [...b.tags].sort(cmpStr)) ||
    cmpStr(a.composer.toLowerCase(), b.composer.toLowerCase()) ||
    cmpStr(a.title.toLowerCase(), b.title.toLowerCase()),
};

// Filter, sort and facet `allScores`. `q` is raw search text; `composer` is
// an exact match ("" for any); every tag in `tags` must be present; `sort` is
// composer, title or tags (anything else keeps the given order).
//
// Facets are context-sensitive: the composer list ignores the composer
// filter (so you can still switch to another), the tag list respects it.
export function filterLibrary(allScores, {
  q = "", composer = "", tags = [], sort = "composer", desc = false,
} = {}) {
  const text = q.trim().toLowerCase();
  const textMatch = (sc) =>
    !text || sc.title.toLowerCase().includes(text) ||
    sc.composer.toLowerCase().includes(text);
  const compMatch = (sc) => !composer || sc.composer === composer;
  const tagMatch = (sc) => tags.every((t) => sc.tags.includes(t));

  const scores = allScores.filter(
    (sc) => textMatch(sc) && compMatch(sc) && tagMatch(sc),
  );
  const sorter = SORTERS[sort];
  if (sorter) scores.sort(desc ? (a, b) => sorter(b, a) : sorter);

  const composers = new Set();
  const tagSet = new Set();
  for (const sc of allScores) {
    if (!textMatch(sc) || !tagMatch(sc)) continue;
    composers.add(sc.composer);
    if (compMatch(sc)) for (const t of sc.tags) tagSet.add(t);
  }
  return {
    scores,
    composers: [...composers].sort(cmpStr),
    tags: [...tagSet].sort(cmpStr),
  };
}

// Scores whose title or composer contains `query`, by composer then title.
export function searchScores(allScores, query) {
  return filterLibrary(allScores, { q: query }).scores;
}
