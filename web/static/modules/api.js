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
