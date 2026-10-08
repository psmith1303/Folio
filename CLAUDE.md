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

## Rules
- PDFs are identified by content hash, so renamed or moved files keep their
  annotations and setlist entries. Don't key anything on the path.
- `config.json` and other runtime `*.json` are local and gitignored. Never
  commit them.
- Don't push before /ship: a push deploys.
