"""Import a real frontend module under Deno with its sibling modules stubbed.

deno_harness.slice_source runs single functions; this runs a whole module
(its import-time code and its wiring between functions). The siblings named
in *stub* are replaced, through an import map, by modules generated from
their real export lists:

- dom.js: every exported element becomes a recording element stub,
  ``__el(name)``, with ``open``, ``classList``, ``click()``, ``focus()`` ...
- state.js: ``getState()`` returns ``globalThis.__state``.
- anything else: every exported function logs ``"<module>:<name>"`` (plus
  its JSON arguments, if any) to ``__log`` and returns
  ``__returns[name](...args)`` when a test provides one.

``document`` is stubbed too: ``addEventListener`` records listeners in
``__docListeners``, ``getElementById`` returns element stubs, and
``querySelector("dialog[open]")`` finds an open ``*Dialog`` element stub.
Generating the stubs from the real exports keeps the tests working across
versions of the module under test.
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from deno_harness import DENO, REPO

MODULES = REPO / "web" / "static" / "modules"

PRELUDE = r"""
globalThis.__log = [];
globalThis.__returns = {};
const __els = new Map();
globalThis.__el = (name) => {
  if (!__els.has(name)) {
    const classes = new Set(["hidden"]);
    __els.set(name, {
      name, open: false, textContent: "", scrollTop: 0, scrollHeight: 500,
      classList: {
        add: (c) => classes.add(c), remove: (c) => classes.delete(c),
        contains: (c) => classes.has(c), toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
      },
      listeners: {},
      addEventListener(ev, fn) { this.listeners[ev] = fn; },
      click() { __log.push("click:" + name); },
      focus() { __log.push("focus:" + name); },
      select() {}, blur() { __log.push("blur:" + name); },
    });
  }
  return __els.get(name);
};
globalThis.__docListeners = {};
globalThis.document = {
  addEventListener: (ev, fn) => { __docListeners[ev] = fn; },
  getElementById: (id) => __el("#" + id),
  querySelector: (sel) => {
    if (sel !== "dialog[open]") throw new Error("unstubbed selector " + sel);
    return [...__els.values()].find((e) => e.name.endsWith("Dialog") && e.open) || null;
  },
};
// A keydown as the browser would deliver it; logs preventDefault.
globalThis.__key = (key, { alt = false, ctrl = false, meta = false, shift = false, tag = "BODY" } = {}) => {
  const target = { tagName: tag, blur: () => __log.push("blur") };
  __docListeners.keydown({
    key, altKey: alt, ctrlKey: ctrl, metaKey: meta, shiftKey: shift, target,
    preventDefault: () => __log.push("preventDefault"),
  });
};
"""


def _exports(src: str) -> tuple[list[str], list[str]]:
    funcs = re.findall(r"^export (?:async )?function (\w+)", src, re.M)
    consts = re.findall(r"^export (?:const|let) (\w+)", src, re.M)
    return funcs, consts


def _stub(name: str, src: str) -> str:
    funcs, consts = _exports(src)
    if name == "dom":
        return "\n".join(f'export const {c} = __el("{c}");' for c in consts)
    if name == "state":
        return "export const getState = () => globalThis.__state;\n" + "\n".join(
            f"export function {f}() {{}}" for f in funcs if f != "getState")
    lines = [
        f"export function {f}(...args) {{\n"
        f'  __log.push("{name}:{f}" + (args.length ? ":" + JSON.stringify(args) : ""));\n'
        f"  const r = __returns.{f}; return r ? r(...args) : undefined;\n}}"
        for f in funcs
    ]
    lines += [f"export const {c} = undefined;" for c in consts]
    return "\n".join(lines)


def run_module(entry: Path, stub: list[str], body: str, *,
               state: dict | None = None, setup: str = "",
               modules_dir: Path = MODULES) -> Any:
    """Import *entry* (a module inside *modules_dir*) with the sibling
    modules named in *stub* replaced, run *body*, and return what it
    prints as JSON. *setup* runs before the import, for globals the modules
    read while loading. The real exports of each stubbed sibling are read
    from MODULES, so an old copy of *entry* elsewhere can be run as well."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        imports = {}
        for name in stub:
            stub_file = tmp_dir / f"{name}.js"
            stub_file.write_text(_stub(name, (MODULES / f"{name}.js").read_text(encoding="utf-8")),
                                 encoding="utf-8")
            imports[(modules_dir / f"{name}.js").as_uri()] = stub_file.as_uri()
        import_map = tmp_dir / "import_map.json"
        import_map.write_text(json.dumps({"imports": imports}), encoding="utf-8")
        script = (PRELUDE
                  + f"globalThis.__state = {json.dumps(state or {})};\n"
                  + setup + "\n"
                  + f'const M = await import("{entry.as_uri()}");\n'
                  + body)
        proc = subprocess.run(
            [DENO, "run", "--no-check", f"--allow-read={REPO},{tmp_dir},{entry.parent}",
             f"--import-map={import_map}", "-"],
            input=script, capture_output=True, text=True, timeout=60,
        )
    assert proc.returncode == 0, f"deno failed:\n{proc.stderr}"
    return json.loads(proc.stdout)

