// ---------------------------------------------------------------------------
// API layer — all fetch calls, retry on network failure, auth redirect
// ---------------------------------------------------------------------------

let _showLoginDialog = null;

export function setLoginHandler(fn) {
  _showLoginDialog = fn;
}

export async function api(url, options = {}) {
  let resp;
  try {
    resp = await fetch(url, options);
  } catch (err) {
    // Network failure — retry once after 1s
    await new Promise((r) => setTimeout(r, 1000));
    resp = await fetch(url, options);
  }
  if (resp.status === 401) {
    if (_showLoginDialog) _showLoginDialog();
    throw new Error("Authentication required");
  }
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json();
}

export async function login(passphrase) {
  const resp = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passphrase }),
  });
  if (!resp.ok) throw new Error("Invalid passphrase");
  return resp.json();
}

export async function getAuthStatus() {
  const resp = await fetch("/api/auth-status");
  return resp.json();
}
