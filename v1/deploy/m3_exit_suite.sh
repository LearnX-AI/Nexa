#!/usr/bin/env bash
# NEXA M3 exit suite (NEXA-M3-010), core NEXA (DGX). Run from the deployed
# checkout as the service user:
#
#   cd /home/admin/Nexa/Nexa && bash v1/deploy/m3_exit_suite.sh
#
# Runs every M3 check that produces evidence against the deployed services and
# the code in this checkout, each into its own log, and writes SUMMARY.md with
# the result line of each. It continues past a failure, so one failing check
# never hides the others. Asks for the sudo password once (to read
# /etc/nexa/*.env and, for check 7, restart the Agent Runtime); nothing secret
# is printed or written.
#
#    1  agent contract: manifests, tools, messages, state, policies  (M3-001, 004-007)
#    2  agent registry and discovery                                 registry_check      (M3-002)
#    3  orchestration engine                                         orchestration_check (M3-003)
#    4  tool and connector gateway                                   tool_gateway_check  (M3-004)
#    5  messaging and event contract                                 messaging_check     (M3-005)
#    6  agent authorization                                          security_check      (M3-007)
#    7  state, sessions and safe resume (restarts the runtime)       state_check         (M3-006)
#    8  observability, tracing and audit                             observability_check (M3-008)
#    9  resilience, offline (73 scenarios) and live                  resilience_eval     (M3-009)
#   10  v0 orchestrator compatibility, offline and live              v0_compat           (M3-010)
set -uo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${NEXA_PY:-/home/admin/Nexa/venv310/bin/python}"
RUNS="${NEXA_EXIT_RUNS:-5}"
OUT="${NEXA_EXIT_OUT:-$HOME/nexa-evidence/m3-exit-$(date -u +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"
SUM="$OUT/SUMMARY.md"

sudo -v || exit 2
set -a
# shellcheck disable=SC1090
. <(sudo cat /etc/nexa/identity.env /etc/nexa/agent-runtime.env)
set +a
cd "$HERE" || exit 2

{
  echo "# NEXA M3 exit suite, $(date -u +%FT%TZ)"
  echo
  echo "Code: \`$(git log --oneline -1)\`; deployed: \`$(tail -1 "$HERE/.nexa_deploy_history" 2>/dev/null | cut -c1-12)\`."
  echo
  echo "| # | Check | Result | Log |"
  echo "|---|---|---|---|"
} > "$SUM"

PASSED=0
FAILED=0
step() {  # $1 number, $2 name, $3 log file, rest: command
  local n="$1" name="$2" log="$3"; shift 3
  echo "[$(date -u +%H:%M:%S)] $n $name"
  "$@" > "$OUT/$log" 2>&1
  local rc=$?
  local line
  line="$(grep -E 'checks passed|scenarios passed|cases passed|thresholds( \(proposed\))?: |thresholds: (MET|NOT MET)|COMPATIBLE|compatibility \((offline|live)\)|^ok ' "$OUT/$log" \
          | tail -1 | tr -d '*|' | cut -c1-110)"
  [ -z "$line" ] && line="exit code $rc"
  if [ $rc -eq 0 ]; then PASSED=$((PASSED + 1)); else FAILED=$((FAILED + 1)); fi
  echo "| $n | $name | $([ $rc -eq 0 ] && echo pass || echo "**fail**") ($line) | $log |" >> "$SUM"
  echo "    $([ $rc -eq 0 ] && echo pass || echo FAIL): $line"
}

contracts() {
  "$PY" -m v1.agent_runtime.manifest validate &&
  "$PY" -m v1.agent_runtime.orchestration.workflow validate &&
  "$PY" -m v1.agent_runtime.policy validate &&
  "$PY" -c "from v1.tool_gateway import contract as ct, handlers as h; t, c = ct.load(handlers=set(h.HANDLERS)); print('ok tools', sorted(t), 'connectors', sorted(c))" &&
  "$PY" -c "import json; from v1.agent_runtime.messaging import contract as mc; assert json.load(open('v1/agent_runtime/schema/nexa-message-1.0.schema.json')) == mc.json_schema(); print('ok nexa.message/1.0 schema')" &&
  "$PY" -c "import json; from v1.agent_runtime.state import contract as sc; assert json.load(open('v1/agent_runtime/schema/nexa-state-1.0.schema.json')) == sc.json_schema(); print('ok nexa.state/1.0 schema')"
}

step 1 "Agent contract: manifests, workflows, policies, tools, messages, state" contracts.log contracts
step 2 "Agent registry and discovery" registry.log "$PY" -m v1.tools.registry_check
step 3 "Orchestration engine" orchestration.log "$PY" -m v1.tools.orchestration_check
step 4 "Tool and connector gateway" tool-gateway.log "$PY" -m v1.tools.tool_gateway_check
step 5 "Messaging and event contract" messaging.log "$PY" -m v1.tools.messaging_check
step 6 "Agent authorization and trust" security.log "$PY" -m v1.tools.security_check
step 7 "State, sessions and safe resume (restarts the Agent Runtime)" state.log "$PY" -m v1.tools.state_check
step 8 "Observability, tracing and audit" observability.log "$PY" -m v1.tools.observability_check
step 9a "Resilience, offline (deterministic scenarios)" resilience-offline.log \
  "$PY" -m v1.tools.resilience_eval --out "$OUT"
step 9b "Resilience, live (real model, $RUNS multi-agent runs)" resilience-live.log \
  "$PY" -m v1.tools.resilience_eval --live --runs "$RUNS" --out "$OUT"
step 10a "v0 orchestrator compatibility, offline" v0-compat-offline.log "$PY" -m v1.tools.v0_compat --out "$OUT"
step 10b "v0 orchestrator compatibility, live" v0-compat-live.log "$PY" -m v1.tools.v0_compat --live --out "$OUT"

{
  echo
  echo "**$PASSED passed, $FAILED failed.** Evidence folder: \`$OUT\`."
} >> "$SUM"
echo
cat "$SUM"
[ "$FAILED" -eq 0 ]
