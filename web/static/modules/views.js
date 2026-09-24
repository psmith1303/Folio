// ---------------------------------------------------------------------------
// View management — switches between the list views and the viewer
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  libraryView, setlistView, recentView, newestView, viewerView,
  btnLibrary, btnSetlists, btnRecent, btnNewest, titleDisplay, pdfContainer,
} from "./dom.js";
import { cleanupScore } from "./viewer.js";
import { loadLibrary } from "./library.js";
import { loadSetlists } from "./setlists.js";
import { renderRecent } from "./recent.js";
import { renderNewest } from "./newest.js";

// The list views the nav bar switches between: the view, its button, what
// fills it each time it is shown, and the element Home/End scroll (if any).
const NAV_VIEWS = {
  library: { el: libraryView, button: btnLibrary, load: loadLibrary, scroller: "library-table-wrap" },
  setlists: { el: setlistView, button: btnSetlists, load: loadSetlists, scroller: "setlist-list-wrap" },
  recent: { el: recentView, button: btnRecent, load: renderRecent, scroller: "recent-table-wrap" },
  newest: { el: newestView, button: btnNewest, load: renderNewest, scroller: null },
};

// The id of the element Home/End scroll in a list view, or null.
export function listScroller(view) {
  return Object.hasOwn(NAV_VIEWS, view) ? NAV_VIEWS[view].scroller : null;
}

// Switch to a list view from anywhere: close the open score if leaving the
// viewer, show the view, then (re)load its contents.
export function navigate(view) {
  if (getState().currentView === "viewer") cleanupScore();
  showView(view);
  NAV_VIEWS[view].load();
}

export function initNavButtons() {
  for (const [view, { button }] of Object.entries(NAV_VIEWS)) {
    button.addEventListener("click", () => navigate(view));
  }
}

export function showView(view) {
  const s = getState();
  s.currentView = view;

  for (const { el, button } of Object.values(NAV_VIEWS)) {
    el.classList.add("hidden");
    button.classList.remove("active");
  }
  viewerView.classList.add("hidden");

  if (Object.hasOwn(NAV_VIEWS, view)) {
    NAV_VIEWS[view].el.classList.remove("hidden");
    NAV_VIEWS[view].button.classList.add("active");
    titleDisplay.textContent = s.appTitle;
  } else if (view === "viewer") {
    viewerView.classList.remove("hidden");
    pdfContainer.focus();
  }
}
