"""modules/dom.js and index.html agree: every element dom.js looks up exists
in the page, and the removed Set Folder controls are gone from both (the
library is fixed at start-up by python -m web --library)."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
DOM_JS = (STATIC / "modules" / "dom.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")


def test_every_dom_js_lookup_is_an_id_in_index_html():
    selectors = re.findall(r'\$\("([^"]+)"\)', DOM_JS)
    assert len(selectors) > 50, "dom.js lookups not found: the pattern needs updating"
    ids = set(re.findall(r'\bid="([^"]+)"', INDEX))
    missing = [s for s in selectors if not (s.startswith("#") and s[1:] in ids)]
    assert missing == [], f"dom.js looks up elements index.html lacks: {missing}"


def test_the_set_folder_controls_are_gone():
    for name in ("btn-set-dir", "dir-dialog", "dir-input", "dir-cancel"):
        assert name not in INDEX, name
    js = {p.relative_to(STATIC).as_posix(): p.read_text(encoding="utf-8")
          for p in STATIC.rglob("*.js") if "lib/" not in p.relative_to(STATIC).as_posix()}
    for path, src in js.items():
        for name in ("btnSetDir", "dirDialog", "dirInput", "showDirDialog", "initDirDialog"):
            assert name not in src, f"{path} still uses {name}"
        # The browser can't set the library: POST /api/library is gone (GET
        # stays: it lists the scores).
        assert not re.search(r'["\']/api/library["\']\s*,\s*\{\s*method:\s*"POST"', src), (
            f"{path} POSTs to /api/library")
