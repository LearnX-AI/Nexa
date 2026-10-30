#!/usr/bin/env bash
# NEXA v1 deploy with a health gate and automatic rollback (NEXA-M1-009).
#
# Usage:  NEXA_HOME=/home/admin/Nexa/Nexa bash v1/deploy/deploy.sh <git-sha>
#
# What it does:
#   1. Records the commit currently deployed (the rollback target).
#   2. Fetches and checks out the requested commit (detached, never a reset, so
#      untracked or locally changed runtime files such as user_memory.json are
#      left alone).
#   3. Installs the pinned requirements into $NEXA_VENV, only if it is set.
#   4. Restarts the NEXA systemd services.
#   5. Waits for every health URL. On success it records the commit in
#      .nexa_deploy_history. On failure it checks out the previous commit,
#      restarts, re-checks health, and exits non-zero.
#
# Configuration comes from the environment, never from this file. Defaults match
# the DEV host. Environment files in /etc/nexa/*.env are not touched.
set -euo pipefail

NEXA_HOME="${NEXA_HOME:-/home/admin/Nexa/Nexa}"
NEXA_BRANCH="${NEXA_BRANCH:-nexa-v1-core}"
# Opt-in: leave empty to skip pip. Point it at a venv used only by v1. Do not
# point it at a venv shared with v0, or the pinned versions upgrade v0 as well.
NEXA_VENV="${NEXA_VENV:-}"
NEXA_SERVICES="${NEXA_SERVICES:-nexa-identity nexa-model-gateway nexa-knowledge nexa-agent-runtime nexa-api-gateway}"
NEXA_HEALTH_URLS="${NEXA_HEALTH_URLS:-http://127.0.0.1:8101/health http://127.0.0.1:8100/health http://127.0.0.1:8103/health http://127.0.0.1:8102/health http://127.0.0.1:8099/ready}"
NEXA_HEALTH_TRIES="${NEXA_HEALTH_TRIES:-20}"
NEXA_HEALTH_WAIT="${NEXA_HEALTH_WAIT:-3}"
NEXA_SYSTEMCTL="${NEXA_SYSTEMCTL:-sudo -n systemctl}"
HISTORY="$NEXA_HOME/.nexa_deploy_history"

REF="${1:?usage: deploy.sh <git-sha-or-ref>}"

log() { echo "[nexa-deploy $(date -u +%FT%TZ)] $*"; }

healthy() {
  local url
  for url in $NEXA_HEALTH_URLS; do
    curl -fsS -m 5 "$url" >/dev/null 2>&1 || return 1
  done
}

wait_healthy() {
  local i
  for ((i = 1; i <= NEXA_HEALTH_TRIES; i++)); do
    if healthy; then return 0; fi
    sleep "$NEXA_HEALTH_WAIT"
  done
  return 1
}

install_requirements() {
  if [ -n "$NEXA_VENV" ] && [ -x "$NEXA_VENV/bin/pip" ]; then
    log "installing pinned requirements into $NEXA_VENV"
    local f
    for f in agent_runtime api_gateway identity knowledge model_gateway; do
      "$NEXA_VENV/bin/pip" install -q -r "v1/$f/requirements.txt"
    done
  else
    log "NEXA_VENV not set (or no pip there), skipping pip install"
  fi
}

restart_services() {
  # shellcheck disable=SC2086
  $NEXA_SYSTEMCTL restart $NEXA_SERVICES
}

cd "$NEXA_HOME"
PREVIOUS="$(git rev-parse HEAD)"
git fetch --quiet origin "$NEXA_BRANCH"
TARGET="$(git rev-parse --verify "${REF}^{commit}")"
log "deploying $TARGET (previous: $PREVIOUS)"

git checkout --quiet --detach "$TARGET"
install_requirements
restart_services

if wait_healthy; then
  echo "$TARGET" >> "$HISTORY"
  log "healthy, deploy recorded"
  exit 0
fi

log "UNHEALTHY after deploy, rolling back to $PREVIOUS"
git checkout --quiet --detach "$PREVIOUS"
install_requirements
restart_services
if wait_healthy; then
  log "rollback complete, services healthy on $PREVIOUS"
else
  log "ROLLBACK ALSO UNHEALTHY. Manual action needed: check journalctl -u nexa-*"
fi
exit 1
