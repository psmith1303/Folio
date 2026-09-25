#!/usr/bin/env bash
# Deploy Folio to its Docker host (p3800).
#
# The source reaches the host by Syncthing, not git: psDATA, including this
# repo and the Docker directory, is a two-way mirror of okapi's copy. So this
#   1. checks the build inputs are committed and the two version strings agree;
#   2. waits until the host's copy of every build input (web/, Dockerfile,
#      .dockerignore, requirements.txt) matches this one byte for byte --
#      building before the mirror catches up would bake in a half-synced tree;
#   3. rebuilds the image and recreates only the folio container;
#   4. verifies: the served version, the container's uid (1000: root-owned
#      files would stop Syncthing), the library, and the public URL.
#
# Usage: scripts/deploy.sh [--check] [--build-only] [--host HOST]
#                          [--timeout SECONDS] [--verify-timeout SECONDS]
#                          [--allow-dirty]
#   --check        steps 1-2 and report what is live; change nothing
#   --build-only   build the image on the host, leave the container alone
#   --host HOST    ssh host to deploy to (default: $FOLIO_HOST or p3800)
#   --timeout N    seconds to wait for the mirror (default 300)
#   --verify-timeout N
#                  seconds to wait for the new version to be served (default 60)
#   --allow-dirty  deploy uncommitted changes to the build inputs

set -euo pipefail

HOST="${FOLIO_HOST:-p3800}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_REPO="${FOLIO_REMOTE_REPO:-$REPO}"                 # same path on both hosts
REMOTE_DOCKER="${FOLIO_REMOTE_DOCKER:-/mnt/z/psDATA/Src/Docker}"
CADDY_ENV="${FOLIO_CADDY_ENV:-/etc/docker-stack/caddy.env}"
PUBLIC_URL="${FOLIO_PUBLIC_URL:-https://folio.66uqs.org}"
TIMEOUT=300
VERIFY_TIMEOUT=60
MODE=deploy
ALLOW_DIRTY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE=check ;;
    --build-only) MODE=build ;;
    --host) HOST="$2"; shift ;;
    --timeout) TIMEOUT="$2"; shift ;;
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

# Per-file hashes of the image's inputs (what the Dockerfile copies, less
# .dockerignore's caches), run identically on both hosts. Syncthing's own
# temporary files (.syncthing.*.tmp, ~syncthing~*.tmp) are left out: a
# transfer in progress already shows as its real file differing, and an
# abandoned one is noise (.dockerignore keeps them out of the image).
INPUTS_CMD='find requirements.txt Dockerfile .dockerignore web -type f ! -path "*/__pycache__/*" ! -name "*.py[cod]" ! -name "*.log" ! -name ".syncthing.*" ! -name "~syncthing~*" -print0 | LC_ALL=C sort -z | xargs -0 sha256sum'
STRAYS_CMD='find web -type f \( -name ".syncthing.*" -o -name "~syncthing~*" \)'

# 1. What is being deployed.
cd "$REPO"
VERSION="$(sed -n 's/^const APP_VERSION = "\(.*\)";$/\1/p' web/static/sw.js)"
SERVER_VERSION="$(sed -n 's/.*title="Folio", version="\([^"]*\)".*/\1/p' web/server.py)"
[[ -n "$VERSION" ]] || die "can't read APP_VERSION from web/static/sw.js"
[[ "$VERSION" == "$SERVER_VERSION" ]] \
  || die "version mismatch: sw.js $VERSION, server.py ${SERVER_VERSION:-?} (bump both)"
DIRTY="$(git status --porcelain -- web Dockerfile .dockerignore requirements.txt)"
if [[ -n "$DIRTY" && $ALLOW_DIRTY -eq 0 ]]; then
  die "uncommitted changes to the build inputs (--allow-dirty to deploy them anyway):
$DIRTY"
fi
say "Folio $VERSION ($(git rev-parse --short HEAD)${DIRTY:+, with uncommitted changes}) → $HOST"

# 2. Wait for the mirror. First make sure the host is reachable and has the
# repo: in the loop below an ssh failure would look like a lagging mirror.
remote true || die "can't reach $HOST over ssh (see the error above)"
remote "test -d '$REMOTE_REPO'" || die "$REMOTE_REPO doesn't exist on $HOST"
LOCAL_SUMS="$(eval "$INPUTS_CMD")"
say "waiting for $HOST's copy to match (up to ${TIMEOUT}s)"
deadline=$((SECONDS + TIMEOUT))
while :; do
  REMOTE_SUMS="$(remote "cd '$REMOTE_REPO' && $INPUTS_CMD" 2>/dev/null || true)"
  [[ "$REMOTE_SUMS" == "$LOCAL_SUMS" ]] && break
  if (( SECONDS >= deadline )); then
    echo "Files that still differ (< here, > $HOST):" >&2
    diff <(echo "$LOCAL_SUMS") <(echo "$REMOTE_SUMS") | grep '^[<>]' | head -20 >&2 || true
    die "$HOST's copy hasn't caught up after ${TIMEOUT}s — is Syncthing running on both?"
  fi
  sleep 5
done
say "mirror is in sync ($(wc -l <<<"$LOCAL_SUMS") files)"
STRAYS="$(remote "cd '$REMOTE_REPO' && $STRAYS_CMD" 2>/dev/null || true)"
if [[ -n "$STRAYS" ]]; then
  echo "note: Syncthing temp files on $HOST (ignored; safe to delete if old):" >&2
  printf '  %s\n' "${STRAYS//$'\n'/$'\n'  }" >&2
fi

live_version() {
  remote "curl -sf localhost:8989/openapi.json" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null \
    || echo "not responding"
}

if [[ $MODE == check ]]; then
  say "live on $HOST: $(live_version); would deploy $VERSION. Nothing changed (--check)."
  exit 0
fi

# 3. Build, and recreate only folio (never the rest of the stack).
COMPOSE="cd '$REMOTE_DOCKER' && CADDY_ENV_FILE='$CADDY_ENV' docker compose"
if [[ $MODE == build ]]; then
  say "building the image on $HOST"
  remote "$COMPOSE build folio"
  say "built; the running container is unchanged (live: $(live_version))"
  exit 0
fi
say "building and recreating folio on $HOST"
remote "$COMPOSE up -d --build folio"

# 4. Verify.
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
