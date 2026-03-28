/* ================================================================== */
/* Folio — Web frontend (entry point)                                 */
/* ================================================================== */

// ---------------------------------------------------------------------------
// Polyfills for older browsers (iPad Safari < 15.4)
// ---------------------------------------------------------------------------

// crypto.randomUUID — Safari 15.4+
if (typeof crypto !== "undefined" && !crypto.randomUUID) {
  crypto.randomUUID = function () {
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (v) => v.toString(16).padStart(2, "0")).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) +
      "-" + h.slice(16, 20) + "-" + h.slice(20);
  };
}

// <dialog> element — Safari 15.4+
(function polyfillDialog() {
  if (typeof HTMLDialogElement !== "undefined") return;

  document.querySelectorAll("dialog").forEach(function (dlg) {
    dlg.style.display = "none";

    var backdrop = document.createElement("div");
    backdrop.className = "dialog-backdrop-polyfill";
    backdrop.style.cssText =
      "display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:999";
    dlg.parentNode.insertBefore(backdrop, dlg);
    dlg._backdrop = backdrop;

    dlg.returnValue = "";

    dlg.showModal = function () {
      this.returnValue = "";
      this.setAttribute("open", "");
      this.style.display = "block";
      this._backdrop.style.display = "block";
    };

    dlg.close = function (val) {
      if (val !== undefined) this.returnValue = val;
      this.removeAttribute("open");
      this.style.display = "none";
      this._backdrop.style.display = "none";
      this.dispatchEvent(new Event("close"));
    };

    Object.defineProperty(dlg, "open", {
      get: function () { return this.hasAttribute("open"); },
    });

    var form = dlg.querySelector('form[method="dialog"]');
    if (form) {
      var lastSubmitter = null;
      form.querySelectorAll('button[type="submit"]').forEach(function (btn) {
        btn.addEventListener("click", function () { lastSubmitter = btn; });
      });
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        dlg.returnValue = lastSubmitter ? (lastSubmitter.value || "") : "";
        lastSubmitter = null;
        dlg.close(dlg.returnValue);
      });
    }
  });
})();

// ---------------------------------------------------------------------------
// Module imports and init
// ---------------------------------------------------------------------------

import { api, getAuthStatus } from "./modules/api.js";
import { dirInput, libraryStatus } from "./modules/dom.js";
import { initTheme } from "./modules/theme.js";
import { initLibraryEvents, loadLibrary, setLoadSetlistsFn } from "./modules/library.js";
import { initViewerEvents, setLoadLibraryFn } from "./modules/viewer.js";
import { initAnnotationEvents } from "./modules/annotations.js";
import { initSetlistEvents, loadSetlists } from "./modules/setlists.js";
import {
  initDialogHandlers, showLoginDialog, showDirDialog,
  setLoadLibraryFn as setDialogLoadLibraryFn, setInitAppFn,
} from "./modules/dialog-handlers.js";
import { initKeyboardShortcuts } from "./modules/keyboard.js";
import { initTouchHandlers } from "./modules/touch.js";

// Wire cross-module callbacks to break circular dependencies
setLoadLibraryFn(loadLibrary);
setDialogLoadLibraryFn(loadLibrary);
setLoadSetlistsFn(loadSetlists);

// Init all event listeners
initTheme();
initLibraryEvents();
initViewerEvents();
initAnnotationEvents();
initSetlistEvents();
initDialogHandlers();
initKeyboardShortcuts();
initTouchHandlers();

// Service worker
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch((err) => {
    console.warn("SW registration failed:", err);
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function initApp() {
  try {
    const cfg = await api("/api/config");
    if (cfg.library_dir && cfg.score_count > 0) {
      dirInput.value = cfg.library_dir;
      await loadLibrary();
    } else {
      showDirDialog(cfg.library_dir || "");
    }
  } catch (err) {
    if (err.message === "Authentication required") return;
    libraryStatus.textContent = `Error: ${err.message}`;
  }
}

setInitAppFn(initApp);

(async function () {
  try {
    const status = await getAuthStatus();
    if (status.auth_required && !status.authenticated) {
      showLoginDialog();
    } else {
      initApp();
    }
  } catch (err) {
    initApp();
  }
})();
