#!/usr/bin/env bash
# Deploy Folio to its Docker host (p3800) by pushing, then check what it serves.
#
# p3800's folio.git deploys on push (2026-10-07): its hook builds the image's
# test stage from the pushed commit (a failure rejects the push), then
# rebuilds and recreates only the folio container from that commit (bin's
# deploy-stack; the image no longer builds from the Syncthing mirror). So this
#   1. checks HEAD is main, the build inputs are committed, and the two
#      version strings agree;
#   2. pushes main to p3800 -- the deploy itself; the hook's output shows as
#      "remote:" lines -- and only then to origin (GitHub), so GitHub never
#      gets a commit p3800 rejected;
#   3. verifies: the served version, the container's uid (1000: root-owned
#      files would stop Syncthing), the library, and the public URL.
#
# Usage: scripts/deploy.sh [--check] [--host HOST] [--verify-timeout SECONDS]
#                          [--allow-dirty]
#   --check        step 1 and report what is live; push nothing
#   --host HOST    ssh host to verify on (default: $FOLIO_HOST or p3800)
#   --verify-timeout N
#                  seconds to wait for the new version to be served (default 60)
#   --allow-dirty  deploy HEAD although the build inputs have uncommitted
#                  changes (those changes are NOT deployed: only commits are)

set -euo pipefail

HOST="${FOLIO_HOST:-p3800}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_REMOTE="${FOLIO_DEPLOY_REMOTE:-p3800}"     # the bare repo with the hook
MIRROR_REMOTE="${FOLIO_MIRROR_REMOTE-origin}"     # GitHub; empty to skip
BRANCH=main
PUBLIC_URL="${FOLIO_PUBLIC_URL:-https://folio.66uqs.org}"
VERIFY_TIMEOUT=60
MODE=deploy
ALLOW_DIRTY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE=check ;;
    --host) HOST="$2"; shift ;;
    --verify-timeout) VERIFY_TIMEOUT="$2"; shift ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    -h|--help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '==> %s\n' "$*"; }
die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }
remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$@"; }

# 1. What is being deployed.
cd "$REPO"
VERSION="$(sed -n 's/^const APP_VERSION = "\(.*\)";$/\1/p' web/static/sw.js)"
SERVER_VERSION="$(sed -n 's/.*title="Folio", version="\([^"]*\)".*/\1/p' web/server.py)"
[[ -n "$VERSION" ]] || die "can't read APP_VERSION from web/static/sw.js"
[[ "$VERSION" == "$SERVER_VERSION" ]] \
  || die "version mismatch: sw.js $VERSION, server.py ${SERVER_VERSION:-?} (bump both)"
[[ "$(git symbolic-ref -q --short HEAD)" == "$BRANCH" ]] \
  || die "not on $BRANCH: only $BRANCH deploys (p3800's hook acts on its HEAD branch)"
DIRTY="$(git status --porcelain -- web Dockerfile .dockerignore requirements.txt)"
if [[ -n "$DIRTY" && $ALLOW_DIRTY -eq 0 ]]; then
  die "uncommitted changes to the build inputs (commit them; --allow-dirty deploys HEAD without them):
$DIRTY"
fi
say "Folio $VERSION ($(git rev-parse --short HEAD)${DIRTY:+; uncommitted changes NOT included}) → $HOST"
remote true || die "can't reach $HOST over ssh (see the error above)"

live_version() {
  remote "curl -sf localhost:8989/openapi.json" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null \
    || echo "not responding"
}

if [[ $MODE == check ]]; then
  say "live on $HOST: $(live_version); would deploy $VERSION. Nothing changed (--check)."
  exit 0
fi

# 2. Push: p3800 tests and deploys, or rejects.
say "pushing $BRANCH to $DEPLOY_REMOTE (its hook tests, then deploys)"
git push "$DEPLOY_REMOTE" "$BRANCH" \
  || die "$DEPLOY_REMOTE rejected the push (or could not be reached) — see the remote: lines above"
if [[ -n "$MIRROR_REMOTE" ]]; then
  git push "$MIRROR_REMOTE" "$BRANCH" \
    || echo "warning: the push to $MIRROR_REMOTE failed; p3800 is deployed regardless" >&2
fi

# 3. Verify.
say "verifying"
deadline=$((SECONDS + VERIFY_TIMEOUT))
until [[ "$(live_version)" == "$VERSION" ]]; do
  (( SECONDS < deadline )) || die "$HOST serves $(live_version) after ${VERIFY_TIMEOUT}s, not $VERSION"
  sleep 2
done
uid="$(remote "docker exec folio id -u")"
[[ "$uid" == 1000 ]] || die "folio runs as uid $uid, not 1000 — root-owned files would stop Syncthing"
scores="$(remote "curl -sf localhost:8989/api/config" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["score_count"])' 2>/dev/null)" \
  || die "/api/config gave no score count — check 'docker logs folio' and last_directory"
(( scores > 0 )) || die "the library is empty — check last_directory is /library"
say "$HOST serves $VERSION as uid 1000, $scores scores"
public="$(curl -sf --max-time 10 "$PUBLIC_URL/openapi.json" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null || true)"
if [[ "$public" == "$VERSION" ]]; then
  say "$PUBLIC_URL serves $VERSION"
else
  echo "warning: $PUBLIC_URL answered '${public:-nothing}' from here (not on the overlay?)" >&2
fi
say "done — iPads pick up $VERSION on their next load"
