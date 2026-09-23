"""Regression tests for the four offline-boot fixes from the 2.9.7 review round.

Each of these executes the REAL source sliced out of the shipped files under
Deno, with only the browser globals it touches stubbed — not a re-typed
description of what the code is supposed to do. A hand-written re-implementation
here would only prove that two independent guesses at the behaviour agree with
each other, which is exactly the drift hazard the rest of this test suite exists
to catch.

F1 — cacheLibrary() only primed /api/library, so a launch that ran it before the
     library was ever set up left /api/config's cached score_count at 0
     permanently; the three call sites that change library_dir/score_count
     didn't refresh it either.
F2 — the service worker cached /api/library even with a query string, so the
     setlist song picker wrote one permanent, offline-useless entry per
     keystroke into a cache nothing ever prunes.
F3 — a cache-write failure shared the network fetch's try/catch, so it could
     turn a successful response into a stale/503 answer while online.
F4 — a stale cached /api/config (predating the running shell) could trigger a
     surprise page reload partway through an offline session.
"""

import json
import re
from pathlib import Path

import pytest

from deno_harness import requires_deno, run_deno, slice_source

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
SW_JS = STATIC / "sw.js"
CACHE_JS = STATIC / "modules" / "cache.js"
APP_JS = STATIC / "app.js"
DIALOG_HANDLERS_JS = STATIC / "modules" / "dialog-handlers.js"
LIBRARY_JS = STATIC / "modules" / "library.js"
VIEWER_JS = STATIC / "modules" / "viewer.js"

def _extract_balanced_parens(source: str, open_paren_index: int) -> str:
    """Return the text strictly inside the parens opening at `open_paren_index`."""
    assert source[open_paren_index] == "("
    depth = 0
    for i in range(open_paren_index, len(source)):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                return source[open_paren_index + 1 : i]
    raise AssertionError("unbalanced parens")


# ---------------------------------------------------------------------------
# F2 — the service worker's cached-GET routing predicate
# ---------------------------------------------------------------------------

def _extract_cached_get_condition(sw_src: str) -> str:
    """Pull the exact boolean expression inside the "Cached API GETs" `if (...)`."""
    anchor = sw_src.index("// Cached API GETs")
    if_kw = sw_src.index("if (", anchor)
    open_paren = if_kw + len("if ")
    return _extract_balanced_parens(sw_src, open_paren)


CACHE_ROUTING_CASES = [
    # (pathname, search, method, expected_cached)
    ("/api/library", "", "GET", True),
    ("/api/library", "?q=beeth", "GET", False),  # the regression: song-picker keystrokes
    ("/api/library", "?", "GET", True),  # WHATWG URL normalizes a bare trailing "?" to empty search
    ("/api/library", "", "POST", False),  # only GET is ever routed here
    ("/api/annotations", "?path=%2Fm%2Fa.pdf", "GET", True),
    ("/api/annotations", "", "GET", True),
    ("/api/config", "", "GET", True),
    ("/api/pdf", "", "GET", False),
    ("/api/library/rescan", "", "GET", False),
]


@requires_deno
@pytest.mark.parametrize(
    "pathname, search, method, expected", CACHE_ROUTING_CASES,
    ids=[f"{p}{s}:{m}" for p, s, m, _ in CACHE_ROUTING_CASES],
)
def test_cached_get_routing(pathname, search, method, expected):
    condition = _extract_cached_get_condition(SW_JS.read_text(encoding="utf-8"))
    script = f"""
const e = {{ request: {{ method: {json.dumps(method)} }} }};
const url = new URL("https://x.test{pathname}{search}");
console.log(JSON.stringify({{ matched: !!({condition}) }}));
"""
    result = run_deno(script)
    assert result["matched"] == expected


# ---------------------------------------------------------------------------
# F3 — a cache-write failure must not discard a successful network response
# ---------------------------------------------------------------------------

FETCH_HANDLER_HARNESS = """
%(consts)s

let __putCalls = 0;
let __putShouldThrow = %(put_throws)s;
const __store = new Map();
const caches = {
  open: async () => ({
    put: async (req, resp) => {
      __putCalls++;
      if (__putShouldThrow) throw new Error("QuotaExceededError");
      __store.set(req.url, resp);
    },
    match: async (req) => __store.get(req.url),
  }),
};

let __fetchShouldThrow = %(fetch_throws)s;
globalThis.fetch = async () => {
  if (__fetchShouldThrow) throw new TypeError("Failed to fetch");
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
};

%(core)s

const request = new Request("https://x.test/api/library");
const resp = await handleApiGetFetch(request);
const body = await resp.text();
console.log(JSON.stringify({
  status: resp.status,
  body,
  putCalls: __putCalls,
}));
"""


def _extract_fetch_handler_core(sw_src: str) -> str:
    # handleApiGetFetch and handleApiGetFetchOffline are the last two
    # functions in the file, so there is no following marker to slice to.
    start = sw_src.index("async function handleApiGetFetch(request)")
    return sw_src[start:]


@requires_deno
class TestCacheWriteFailureIsolation:
    def _run(self, *, fetch_throws: bool, put_throws: bool) -> dict:
        core = _extract_fetch_handler_core(SW_JS.read_text(encoding="utf-8"))
        script = FETCH_HANDLER_HARNESS % {
            "consts": 'const API_CACHE = "folio-api-v1";',
            "put_throws": "true" if put_throws else "false",
            "fetch_throws": "true" if fetch_throws else "false",
            "core": core,
        }
        return run_deno(script)

    def test_successful_fetch_and_successful_cache_write(self):
        result = self._run(fetch_throws=False, put_throws=False)
        assert result["status"] == 200
        assert json.loads(result["body"]) == {"ok": True}
        assert result["putCalls"] == 1

    def test_cache_write_failure_still_returns_the_network_response(self):
        """The regression: previously this returned 503, discarding a real 200."""
        result = self._run(fetch_throws=False, put_throws=True)
        assert result["status"] == 200, (
            f"got {result['status']} — a cache.put() failure must not turn a "
            "successful network response into an error"
        )
        assert json.loads(result["body"]) == {"ok": True}

    def test_network_failure_with_nothing_cached_returns_offline_503(self):
        result = self._run(fetch_throws=True, put_throws=False)
        assert result["status"] == 503
        assert json.loads(result["body"]) == {"error": "Offline"}


# ---------------------------------------------------------------------------
# F4 — a stale cached config must not trigger a reload while offline
# ---------------------------------------------------------------------------

STALE_SHELL_HARNESS = """
let __scheduledReload = false;
const navigator = { onLine: %(online)s, serviceWorker: {
  getRegistration: async () => ({ update: async () => {} }),
} };
const window = { location: { reload: () => { throw new Error("should not be called synchronously"); } } };
const sessionStorage = { _d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = v; } };
function getServiceWorkerVersion() { return Promise.resolve(%(sw_version)s); }
function setTimeout(fn, ms) { __scheduledReload = true; return 0; }

%(core)s

await reloadIfShellStale(%(server_version)s);
console.log(JSON.stringify({ scheduledReload: __scheduledReload }));
"""


def _extract_reload_if_shell_stale(app_src: str) -> str:
    return slice_source(
        app_src,
        "async function reloadIfShellStale(serverVersion) {",
        "// ---------------------------------------------------------------------------\n// Boot",
    )


@requires_deno
class TestStaleShellOfflineGuard:
    def _run(self, *, online: bool, sw_version, server_version) -> dict:
        core = _extract_reload_if_shell_stale(APP_JS.read_text(encoding="utf-8"))
        script = STALE_SHELL_HARNESS % {
            "online": "true" if online else "false",
            "sw_version": json.dumps(sw_version),
            "server_version": json.dumps(server_version),
            "core": core,
        }
        return run_deno(script)

    def test_mismatch_while_offline_does_not_schedule_a_reload(self):
        """The regression: a stale cached config used to reload mid-offline-session."""
        result = self._run(online=False, sw_version="2.9.6", server_version="2.9.7")
        assert result["scheduledReload"] is False, (
            "reloadIfShellStale scheduled a reload while navigator.onLine is "
            "false — offline, there is nothing to self-heal, and this drops "
            "the user's open score"
        )

    def test_mismatch_while_online_still_schedules_a_reload(self):
        """The guard must not swallow the legitimate self-heal case."""
        result = self._run(online=True, sw_version="2.9.6", server_version="2.9.7")
        assert result["scheduledReload"] is True

    def test_matching_versions_never_schedule_a_reload(self):
        result = self._run(online=True, sw_version="2.9.7", server_version="2.9.7")
        assert result["scheduledReload"] is False


# ---------------------------------------------------------------------------
# F1 — the cached /api/config must not go stale
# ---------------------------------------------------------------------------

CONFIG_REFRESH_HARNESS = """
const CACHE_AVAILABLE = %(cache_available)s;
let __fetched = [];
globalThis.fetch = async (url) => {
  __fetched.push(url);
  return new Response("{}", { status: 200 });
};

%(core)s

%(call)s
// Allow the fire-and-forget fetch() microtask in refreshCachedConfig to run.
await new Promise((r) => setTimeout(r, 0));
console.log(JSON.stringify({ fetched: __fetched }));
"""


def _extract_cache_functions(cache_src: str) -> str:
    return slice_source(
        cache_src,
        "export function refreshCachedConfig() {",
        "// ---------------------------------------------------------------------------\n// UI",
    )


@requires_deno
class TestConfigCacheStaysFresh:
    def _run(self, *, cache_available: bool, call: str) -> dict:
        core = _extract_cache_functions(CACHE_JS.read_text(encoding="utf-8"))
        script = CONFIG_REFRESH_HARNESS % {
            "cache_available": "true" if cache_available else "false",
            "core": core,
            "call": call,
        }
        return run_deno(script)

    def test_cache_library_primes_config_as_well_as_library(self):
        """The regression: 'Refresh Library Cache' left a stale config cached."""
        result = self._run(cache_available=True, call="await cacheLibrary();")
        assert "/api/library" in result["fetched"]
        assert "/api/config" in result["fetched"], (
            "cacheLibrary() no longer refreshes /api/config; a cold offline "
            "launch can read a stale score_count and skip the cached library"
        )

    def test_refresh_cached_config_re_fetches_config(self):
        result = self._run(cache_available=True, call="refreshCachedConfig();")
        assert result["fetched"] == ["/api/config"]

    def test_refresh_cached_config_is_a_noop_without_the_cache(self):
        """No point re-fetching to warm a cache that cannot exist."""
        result = self._run(cache_available=False, call="refreshCachedConfig();")
        assert result["fetched"] == []


# Source-level wiring: every place that can change library_dir/score_count
# must call refreshCachedConfig() afterwards, or the fix above is unreachable
# from the paths that actually go stale.
WIRING_CASES = [
    (DIALOG_HANDLERS_JS, "dirDialog.addEventListener(", "set-folder dialog"),
    (LIBRARY_JS, "btnReset.addEventListener(", "reset/rescan button"),
]


class TestConfigRefreshIsWiredIn:
    @pytest.mark.parametrize("path, anchor, label", WIRING_CASES, ids=[c[2] for c in WIRING_CASES])
    def test_handler_calls_refresh_after_reloading_the_library(self, path, anchor, label):
        """Paren-balanced, not a search for a closing "});" — the handler
        bodies contain nested calls (api(...) with its own trailing "});")
        that a naive string search finds before the real end of the block.
        """
        src = path.read_text(encoding="utf-8")
        call_start = src.index(anchor)
        open_paren = call_start + len(anchor) - 1
        block = _extract_balanced_parens(src, open_paren)
        assert "refreshCachedConfig()" in block, (
            f"the {label} handler in {path.name} reloads the library but never "
            "calls refreshCachedConfig(), so a cached /api/config can go stale"
        )

    def test_both_rescan_on_missing_file_paths_in_viewer_refresh_config(self):
        src = VIEWER_JS.read_text(encoding="utf-8")
        occurrences = [m.start() for m in re.finditer(r'no longer available', src)]
        assert len(occurrences) == 2, "expected exactly the two known rescan-on-404 sites"
        for pos in occurrences:
            block = src[max(0, pos - 300):pos]
            assert "refreshCachedConfig()" in block, (
                "a rescan-on-404 path in viewer.js reloads the library without "
                "refreshing the cached /api/config"
            )


# ---------------------------------------------------------------------------
# F2 / general hygiene — service-worker cache lifecycle and version lockstep
#
# Source-level (no Deno needed): these read sw.js and server.py directly
# rather than executing them, since what they guard is presence/shape of a
# declaration, not runtime behaviour under inputs.
# ---------------------------------------------------------------------------

class TestServiceWorkerOfflineBoot:
    """Guards the two service-worker defects that kept offline mode partial."""

    @pytest.fixture(scope="class")
    def sw_js(self):
        return SW_JS.read_text(encoding="utf-8")

    def test_config_endpoint_is_cached(self, sw_js):
        """A cold offline launch dies at boot without this.

        initApp() awaits /api/config before it will call loadLibrary(); if the
        service worker passes that request through to a dead network, the boot
        aborts in its catch and the cached library is never requested at all.
        """
        cached_get = sw_js[sw_js.index("// Cached API GETs"):sw_js.index("// Other API calls")]
        assert '"/api/config"' in cached_get, (
            "/api/config is not in the service worker's cached-GET list; a cold "
            "offline start will abort in initApp() before loadLibrary() runs"
        )

    def test_api_cache_is_not_version_keyed(self, sw_js):
        """Version-keying the API cache wipes the offline library every release."""
        line = next(
            l for l in sw_js.splitlines() if l.startswith("const API_CACHE")
        )
        assert "APP_VERSION" not in line, (
            f"{line.strip()!r} is keyed by version, so activate() discards the "
            "cached library snapshot on every release"
        )

    def test_activate_preserves_the_api_cache(self, sw_js):
        """activate() deletes every cache it does not explicitly spare.

        Asserts the actual sparing condition, not just that API_CACHE is
        mentioned somewhere in the sweep — an inverted filter that deletes
        ONLY the API cache (`n === API_CACHE`) would satisfy a bare
        "in sweep" check while doing the opposite of what is required.
        """
        sweep = sw_js[sw_js.index('addEventListener("activate"'):]
        sweep = sweep[:sweep.index("clients.claim")]
        assert "n !== API_CACHE" in sweep, (
            "activate()'s cache sweep does not spare API_CACHE, so the offline "
            "library is deleted on the next version bump"
        )


SERVER_PY = STATIC.parent / "server.py"


class TestVersionLockstep:
    """web/server.py `version=` and sw.js APP_VERSION drive the stale-shell
    self-heal (reloadIfShellStale in app.js): the client compares them to
    detect an out-of-date shell. They live in two files with no shared
    source and have drifted before — this is the guard against that.
    """

    def test_versions_match(self):
        server_src = SERVER_PY.read_text(encoding="utf-8")
        sw_src = SW_JS.read_text(encoding="utf-8")
        server_match = re.search(r'version="(\d+\.\d+\.\d+)"', server_src)
        sw_match = re.search(r'APP_VERSION = "(\d+\.\d+\.\d+)"', sw_src)
        assert server_match, "no version=\"X.Y.Z\" found in web/server.py"
        assert sw_match, "no APP_VERSION = \"X.Y.Z\" found in web/static/sw.js"
        assert server_match.group(1) == sw_match.group(1), (
            f"web/server.py version={server_match.group(1)!r} but sw.js "
            f"APP_VERSION={sw_match.group(1)!r} — bump them together or the "
            "stale-shell self-heal in app.js compares against the wrong value"
        )
