"""Guard: sw.js's SHELL_URLS precache list must include every module that
any shipped module (or app.js) statically imports.

This is the general version of the gap the 2.16.1 pen-style refactor almost
shipped with: annotations.js grew an `import ... from "./pen-style.js"` and
the new file needed adding to SHELL_URLS by hand, alongside it, or an
offline launch would 404 on it once the old cached copy of annotations.js
(without the import) fell out of the API cache. test_pen_style.py's own
wiring tests don't catch this -- they import modules directly by file, not
through the service worker's cache -- so this checks the two source files
against each other directly, statically, with no Deno needed.

Pure text/regex, not a JS harness: import statements are ordinary ES module
syntax, and SHELL_URLS is a plain array literal in sw.js.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
MODULES = STATIC / "modules"
APP_JS = STATIC / "app.js"
SW_JS = STATIC / "sw.js"

# `from "./x.js"` (however the braces around it wrap across lines -- the
# `from "..."` clause itself is always on one line) and the bare
# `import "./x.js";` form (side-effect-only imports, e.g. cache.js).
IMPORT_FROM_RE = re.compile(r'from\s+"(\.[^"]+)"')
IMPORT_BARE_RE = re.compile(r'^\s*import\s+"(\.[^"]+)"', re.M)


def _shell_urls() -> set[str]:
    src = SW_JS.read_text(encoding="utf-8")
    m = re.search(r"const SHELL_URLS = \[(.*?)\];", src, re.S)
    assert m, "SHELL_URLS = [...] not found in sw.js -- did it get renamed?"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _relative_import_targets(path: Path) -> set[str]:
    """Every relative *.js import target in *path*, resolved to the public
    path sw.js would cache it under (e.g. "/modules/pen-style.js")."""
    src = path.read_text(encoding="utf-8")
    raw = set(IMPORT_FROM_RE.findall(src)) | set(IMPORT_BARE_RE.findall(src))
    out = set()
    for t in raw:
        if not t.endswith(".js"):
            continue
        resolved = (path.parent / t).resolve()
        out.add("/" + str(resolved.relative_to(STATIC)))
    return out


def test_every_static_import_is_in_the_precache_list():
    shell = _shell_urls()
    missing = {}
    for src_file in [APP_JS, *sorted(MODULES.glob("*.js"))]:
        gap = _relative_import_targets(src_file) - shell
        if gap:
            missing[f"web/static/{src_file.relative_to(STATIC)}"] = sorted(gap)
    assert not missing, (
        "module(s) imported but missing from sw.js SHELL_URLS (an offline "
        f"launch will 404 fetching them): {missing}"
    )


def test_the_import_parser_actually_finds_something():
    """Sanity check on the regexes themselves: if they stopped matching
    anything (e.g. the shipped source moved to a different import style),
    the test above would pass vacuously and stop guarding anything."""
    total = sum(len(_relative_import_targets(f))
                for f in [APP_JS, *MODULES.glob("*.js")])
    assert total > 20
