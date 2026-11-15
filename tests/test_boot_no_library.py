"""app.js initApp with an empty or missing library. The browser can no longer
pick a folder, so the status line must say what to fix: a server started
without --library, or a library with no scores in it (e.g. a missing mount).
Runs the shipped initApp under Deno with its imports stubbed."""
import json

import pytest

from deno_harness import REPO, requires_deno, run_deno, slice_source

APP_JS = (REPO / "web" / "static" / "app.js").read_text(encoding="utf-8")
INIT_APP = slice_source(APP_JS, "async function initApp()", "\ninitApp();")


def _boot(cfg):
    script = f"""
const calls = [];
const cfg = {json.dumps(cfg)};
const api = async (url) => {{ calls.push(url); return cfg; }};
const libraryStatus = {{ textContent: "" }};
const titleDisplay = {{ textContent: "" }};
const state = {{}};
const getState = () => state;
const setKeybindings = () => {{}};
const reloadIfShellStale = () => {{}};
const loadLibrary = async () => calls.push("loadLibrary");
const syncPendingAnnotations = () => calls.push("syncPendingAnnotations");
const requestPersistentStorage = () => {{}};
const syncOfflineSet = () => calls.push("syncOfflineSet");
const showDirDialog = () => calls.push("showDirDialog");
{INIT_APP}
await initApp();
console.log(JSON.stringify({{ status: libraryStatus.textContent, calls }}));
"""
    return run_deno(script)


@requires_deno
@pytest.mark.parametrize("cfg, status", [
    ({"library_dir": "", "score_count": 0}, "No library: start Folio with --library."),
    ({"library_dir": "/library", "score_count": 0}, "No scores in the library."),
], ids=["no --library", "empty library"])
def test_an_unusable_library_says_what_is_wrong(cfg, status):
    out = _boot({**cfg, "version": "9.9.9"})
    assert out["status"] == status
    # Nothing tries to load a library, or open the old folder dialog.
    assert out["calls"] == ["/api/config"]


@requires_deno
def test_a_library_with_scores_loads():
    out = _boot({"library_dir": "/library", "score_count": 3, "version": "9.9.9"})
    assert out["status"] == ""
    assert out["calls"][:3] == ["/api/config", "loadLibrary", "syncPendingAnnotations"]
