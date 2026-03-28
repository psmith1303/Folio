// ---------------------------------------------------------------------------
// View management — switches between library, setlists, and viewer
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  libraryView, setlistView, viewerView,
  btnLibrary, btnSetlists, btnBack, titleDisplay, pdfContainer,
} from "./dom.js";

export function showView(view) {
  const s = getState();
  s.currentView = view;

  libraryView.classList.add("hidden");
  setlistView.classList.add("hidden");
  viewerView.classList.add("hidden");
  btnLibrary.classList.add("hidden");
  btnSetlists.classList.add("hidden");
  btnBack.classList.add("hidden");
  btnLibrary.classList.remove("active");
  btnSetlists.classList.remove("active");

  switch (view) {
    case "library":
      libraryView.classList.remove("hidden");
      btnLibrary.classList.remove("hidden");
      btnSetlists.classList.remove("hidden");
      btnLibrary.classList.add("active");
      titleDisplay.textContent = "";
      break;
    case "setlists":
      setlistView.classList.remove("hidden");
      btnLibrary.classList.remove("hidden");
      btnSetlists.classList.remove("hidden");
      btnSetlists.classList.add("active");
      titleDisplay.textContent = "";
      break;
    case "viewer":
      viewerView.classList.remove("hidden");
      btnBack.classList.remove("hidden");
      pdfContainer.focus();
      break;
  }
}
