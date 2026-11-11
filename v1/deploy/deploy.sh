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
#   2a. Release gate (NEXA-M2-008): if the retrieval code (v1/knowledge,
#      v1/model_gateway, excluding the evaluation data) differs from what is
#      deployed, a passing corpus evaluation for exactly that code must have
#      been recorded on this host by v1/tools/nexa_eval.py corpus --record-gate,
#      or the deploy stops before anything changes. NEXA_SKIP_EVAL_GATE=1 with
#      NEXA_SKIP_EVAL_REASON overrides it; every override is logged to
#      .nexa_eval_gate_overrides.
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
NEXA_EVAL_GATE_DIR="${NEXA_EVAL_GATE_DIR:-$HOME/.nexa/eval-gates}"

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

# Identifies the retrieval code at a commit. Must match gate_key() in
# v1/tools/nexa_eval.py.
retrieval_key() {
  git ls-tree -r "$1" -- v1/knowledge v1/model_gateway \
    | grep -v $'\tv1/knowledge/benchmark/eval/' | sha1sum | cut -c1-16
}

eval_gate() {
  local prev_key target_key record
  prev_key="$(retrieval_key "$PREVIOUS")"
  target_key="$(retrieval_key "$TARGET")"
  if [ "$prev_key" = "$target_key" ]; then
    log "eval gate: retrieval code unchanged ($target_key), no evaluation needed"
    return 0
  fi
  record="$NEXA_EVAL_GATE_DIR/$target_key.json"
  if [ -f "$record" ] && grep -q '"passed": true' "$record"; then
    log "eval gate: passing evaluation recorded for $target_key ($record)"
    return 0
  fi
  if [ "${NEXA_SKIP_EVAL_GATE:-}" = "1" ]; then
    log "WARNING eval gate OVERRIDDEN for $target_key: ${NEXA_SKIP_EVAL_REASON:-no reason given}"
    echo "$(date -u +%FT%TZ) $TARGET $target_key ${USER:-?} ${NEXA_SKIP_EVAL_REASON:-no reason given}" \
      >> "$NEXA_HOME/.nexa_eval_gate_overrides"
    return 0
  fi
  log "eval gate: retrieval code changed ($prev_key -> $target_key) and no passing evaluation is recorded"
  log "run on this host, from a checkout of $TARGET:"
  log "  python v1/tools/nexa_eval.py corpus --corpus <dir> --out <dir> --record-gate"
  return 1
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
if ! eval_gate; then
  log "deploy refused, nothing changed"
  exit 3
fi

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
