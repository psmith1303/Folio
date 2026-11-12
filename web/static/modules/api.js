// ---------------------------------------------------------------------------
// API layer — all fetch calls, retry on network failure
// ---------------------------------------------------------------------------

export async function api(url, options = {}) {
  options.cache = "no-store";
  let resp;
  try {
    resp = await fetch(url, options);
  } catch (err) {
    // Network failure — retry once after 1s
    await new Promise((r) => setTimeout(r, 1000));
    resp = await fetch(url, options);
  }
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json();
}

// Ask the controlling service worker *msg* (e.g. { type: "GET_VERSION" })
// and resolve to its reply, or null if there's no controller yet (first
// install), it doesn't answer within 1.5 s, or posting fails.
export function askServiceWorker(msg) {
  const sw = navigator.serviceWorker && navigator.serviceWorker.controller;
  if (!sw) return Promise.resolve(null);
  return new Promise((resolve) => {
    const ch = new MessageChannel();
    const done = (reply) => {
      clearTimeout(timer);
      ch.port1.close();
      resolve(reply);
    };
    const timer = setTimeout(() => done(null), 1500);
    ch.port1.onmessage = (e) => done(e.data || null);
    try {
      sw.postMessage(msg, [ch.port2]);
    } catch (_) {
      done(null);
    }
  });
}
