// ---------------------------------------------------------------------------
// Touch gestures — swipe, double-tap, scroll-boundary page turns
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import { annotCanvas1, annotCanvas2, pdfContainer } from "./dom.js";
import { nextPage, prevPage, isFullscreen, applyFullscreen } from "./viewer.js";

const SWIPE_THRESHOLD = 50;

export function initTouchHandlers() {
  let touchStartX = null;
  let touchStartY = null;

  for (const ac of [annotCanvas1, annotCanvas2]) {
    ac.addEventListener("touchstart", (e) => {
      const s = getState();
      if (s.activeTool !== "nav" || isFullscreen() || s.displayMode === "wide") return;
      if (e.touches.length !== 1) return;
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
    }, { passive: true });

    ac.addEventListener("touchend", (e) => {
      const s = getState();
      if (s.activeTool !== "nav" || isFullscreen() || s.displayMode === "wide" || touchStartX === null) return;
      if (e.changedTouches.length !== 1) return;
      const dx = e.changedTouches[0].clientX - touchStartX;
      const dy = e.changedTouches[0].clientY - touchStartY;
      touchStartX = null;
      touchStartY = null;

      if (Math.abs(dx) > SWIPE_THRESHOLD && Math.abs(dx) > Math.abs(dy)) {
        if (dx < 0) nextPage();
        else prevPage();
      }
    }, { passive: true });
  }

  // Double-tap exits pseudo-fullscreen
  pdfContainer.addEventListener("dblclick", () => {
    if (getState().pseudoFullscreen) applyFullscreen(false);
  });

  // Scroll-boundary page turns in fullscreen wide mode
  let scrollPageTurnCooldown = false;

  pdfContainer.addEventListener("scroll", () => {
    const s = getState();
    if (!isFullscreen() || !s.pdfDoc || scrollPageTurnCooldown) return;
    if (s.displayMode !== "wide") return;

    const el = pdfContainer;
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 2;
    const atTop = el.scrollTop <= 0;

    if (atBottom && s.currentPage < s.totalPages) {
      scrollPageTurnCooldown = true;
      nextPage();
      setTimeout(() => { scrollPageTurnCooldown = false; }, 500);
    } else if (atTop && s.currentPage > 1) {
      scrollPageTurnCooldown = true;
      s.scrollToBottomAfterRender = true;
      prevPage();
      setTimeout(() => { scrollPageTurnCooldown = false; }, 500);
    }
  }, { passive: true });
}
