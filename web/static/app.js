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

import { getState } from "./modules/state.js";
import { api, getAuthStatus } from "./modules/api.js";
import { dirInput, libraryStatus, titleDisplay } from "./modules/dom.js";
import { initTheme } from "./modules/theme.js";
import { initLibraryEvents, loadLibrary, setLoadSetlistsFn } from "./modules/library.js";
import { initViewerEvents, setLoadLibraryFn, openScore, cleanupScore } from "./modules/viewer.js";
import { initAnnotationEvents } from "./modules/annotations.js";
import { initSetlistEvents, loadSetlists } from "./modules/setlists.js";
import {
  initDialogHandlers, showLoginDialog, showDirDialog,
  setLoadLibraryFn as setDialogLoadLibraryFn, setInitAppFn,
} from "./modules/dialog-handlers.js";
import { initKeyboardShortcuts, setKeybindings } from "./modules/keyboard.js";
import { initTouchHandlers } from "./modules/touch.js";
import { initCacheUI } from "./modules/cache.js";
import { initRecentEvents, setRecentCallbacks } from "./modules/recent.js";
import { initNewestEvents, setNewestCallbacks } from "./modules/newest.js";

// Wire cross-module callbacks to break circular dependencies
setLoadLibraryFn(loadLibrary);
setDialogLoadLibraryFn(loadLibrary);
setLoadSetlistsFn(loadSetlists);
setRecentCallbacks(openScore, cleanupScore);
setNewestCallbacks(openScore, cleanupScore);

// Surface any unhandled async failure as a viewer toast — keeps the user in
// the viewer instead of letting silent errors corrupt later interactions.
//
// "Script error." with no filename/lineno is WebKit's cross-origin mask
// (workers, indirect handlers). It's pure noise to the user, so suppress
// the toast in that case — still logs to console and, if enabled, the
// server. Enable on iPad via DevTools-equivalent:
//   localStorage.folioRemoteLog = "1"
// then `tail` the Folio server stdout from a machine that has a console.
import { showToast } from "./modules/viewer.js";

let _lastRemoteLogTs = 0;
function remoteLog(level, message, detail) {
  if (typeof localStorage === "undefined") return;
  if (localStorage.folioRemoteLog !== "1") return;
  const now = Date.now();
  if (now - _lastRemoteLogTs < 500) return;  // throttle bursts
  _lastRemoteLogTs = now;
  fetch("/api/clientlog", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      level,
      message: String(message || ""),
      detail: String(detail || ""),
      url: location.href,
      ua: navigator.userAgent,
    }),
  }).catch(() => { /* swallow — don't recurse on log failure */ });
}

function isBareScriptError(e) {
  return e && e.message === "Script error." && !e.filename;
}

window.addEventListener("unhandledrejection", (e) => {
  const reason = e.reason;
  const msg = (reason && (reason.message || reason.toString())) || "unknown error";
  const stack = (reason && reason.stack) || "";
  console.error("Unhandled rejection:", reason);
  remoteLog("error", `unhandledrejection: ${msg}`, stack);
  if (getState().currentView === "viewer") showToast(`Background error: ${msg}`);
});

window.addEventListener("error", (e) => {
  const stack = (e.error && e.error.stack) || "";
  const loc = e.filename ? `${e.filename}:${e.lineno}:${e.colno}` : "";
  const detail = loc ? `${e.message} @ ${loc}` : e.message || "unknown";
  console.error("Window error:", detail, e.error);
  remoteLog("error", detail, stack);
  if (isBareScriptError(e)) return;  // toast would be noise — no useful info
  if (getState().currentView === "viewer") showToast(`Error: ${detail}`);
});

// Init all event listeners
initTheme();
initLibraryEvents();
initViewerEvents();
initAnnotationEvents();
initSetlistEvents();
initDialogHandlers();
initKeyboardShortcuts();
initTouchHandlers();
initCacheUI();
initRecentEvents();
initNewestEvents();

// Service worker
if ("serviceWorker" in navigator) {
  // Auto-reload when a new SW takes control (ensures fresh shell + data)
  let reloading = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!reloading) {
      reloading = true;
      window.location.reload();
    }
  });

  navigator.serviceWorker.register("/sw.js").then((reg) => {
    // Force an immediate update check instead of waiting for the browser's
    // lazy heuristic (navigation-in-scope / ~24h). This is what actually
    // prevents stale shells: a bumped sw.js installs, skipWaiting fires,
    // and the controllerchange handler above reloads into the new build.
    const checkForUpdate = () => reg.update().catch(() => {});
    checkForUpdate();
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") checkForUpdate();
    });
  }).catch((err) => {
    console.warn("SW registration failed:", err);
  });
}

// Ask the controlling service worker which build it's serving. Returns null
// if there's no controller yet (first install) or it doesn't answer.
function getServiceWorkerVersion() {
  const sw = navigator.serviceWorker && navigator.serviceWorker.controller;
  if (!sw) return Promise.resolve(null);
  return new Promise((resolve) => {
    const ch = new MessageChannel();
    const timer = setTimeout(() => resolve(null), 1500);
    ch.port1.onmessage = (e) => {
      clearTimeout(timer);
      resolve((e.data && e.data.version) || null);
    };
    try {
      sw.postMessage({ type: "GET_VERSION" }, [ch.port2]);
    } catch (_) {
      clearTimeout(timer);
      resolve(null);
    }
  });
}

// If the running shell is older than what the server now serves, force the
// SW to update. The controllerchange handler reloads once it takes over;
// the timed fallback covers the case where that never fires, guarded by
// sessionStorage so we reload at most once per server version (no loop).
async function reloadIfShellStale(serverVersion) {
  if (!serverVersion || !("serviceWorker" in navigator)) return;
  const swVersion = await getServiceWorkerVersion();
  if (!swVersion || swVersion === serverVersion) return;

  console.warn(
    `Stale shell: running ${swVersion}, server is ${serverVersion} — updating`,
  );
  try {
    const reg = await navigator.serviceWorker.getRegistration();
    if (reg) reg.update().catch(() => {});
  } catch (_) {
    // getRegistration can reject in some privacy modes; fall through.
  }

  let alreadyTried = null;
  try {
    alreadyTried = sessionStorage.getItem("folio.staleReload");
  } catch (_) {
    // sessionStorage unavailable (private mode) — skip the loop guard;
    // the controllerchange path is still in play.
  }
  if (alreadyTried === serverVersion) return;
  try {
    sessionStorage.setItem("folio.staleReload", serverVersion);
  } catch (_) {
    // ignore
  }
  setTimeout(() => window.location.reload(), 3000);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function initApp() {
  try {
    const cfg = await api("/api/config");
    if (cfg.keybindings) setKeybindings(cfg.keybindings);
    if (cfg.version) {
      const s = getState();
      s.appTitle = `Folio v${cfg.version}`;
      titleDisplay.textContent = s.appTitle;
      reloadIfShellStale(cfg.version);
    }
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
