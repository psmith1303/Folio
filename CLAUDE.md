# Folio: notes for Claude

A web app for viewing and annotating PDF music scores in a browser, designed
for an iPad and Apple Pencil. FastAPI backend (`web.server:app`), plain JS
front end. It runs as a container on p3800, behind Caddy at
https://folio.66uqs.org. Read `README.org` for an overview and `README.md` for
the full feature list and run instructions.

## Layout
- `web/`: server (`server.py`, `core.py`) and front end (`static/`; pdf.js
  is vendored under `static/lib/pdfjs/`, so don't edit it).
- `scripts/`: maintenance (stamps, tag sorting, migrations).
- `docs/`: plan, setlist file format, manual test checklist.
- `tests/`: pytest suite. `setlist-editor.el`: the Emacs helper.

## Test and deploy
- Tests: `pytest` from the repo root (requirements-dev.txt).
- **Deploy = `git push p3800 master`.** The hook builds `folio-test` from
  the pushed commit (a failure rejects the push), then runs
  `up -d --build folio` from `/srv/apps/folio`.
- After a front-end change, use `docs/manual-test-checklist.org` in a real
  browser. The pytest suite doesn't cover the page.
- There's no node or browser here, only Deno. Test front-end logic by running
  the real shipped JS under Deno from pytest: `tests/deno_harness.py`
  (`run_deno`, `slice_source`) and `tests/js_module_harness.py` (`run_module`
  with generated sibling stubs). Put new pure logic in `modules/utils.js`.
  Deno gotchas: `caches` and `localStorage` need `Object.defineProperty`, and
  `localStorage` is real and persists across runs. Two sliced functions with one
  name: wrap one in an IIFE, don't rename it.
- Tests must not read git history or gitignored files: /ship runs them from an
  export with no `.git`.
- The library and checkout on `/mnt/z` are case-insensitive drvfs; `tmp_path`
  isn't. Filename code needs one test on the real drive (`tests/test_sort_tags.py`).
  Fake PDFs need unique bytes, or the hash index stays empty.

## Loop preferences
Keep plan steps whole (Peter chose "one step, as planned" and "one commit" every
time); at /ship offer a fix/refactor split once and recommend one commit when the
plan item covers the fix. A separate fix Peter asked for still gets its own commit.

## Rules
- PDFs are identified by content hash, so renamed or moved files keep their
  annotations and setlist entries. Don't key anything on the path.
- `config.json` and other runtime `*.json` are local and gitignored. Never
  commit them.
- Don't push before /ship: a push deploys.
- Keep login/auth UI text generic ("Enter access code:"): never hint at the
  passphrase format.
- Update README.md in the same commit as the code change it describes.
