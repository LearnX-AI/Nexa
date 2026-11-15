#!/usr/bin/env bash
# Classroom load test on a school server (NEXA-M2-009). Run as root:
#
#   sudo bash /opt/nexa-edge/app/v1/edge/deploy/loadtest.sh
#
# Two configurations, each at several class sizes. Before every run the
# knowledge service is restarted, so no answer cache or counters carry over:
#   before  today's settings: no answer queue, model gateway timeout 60 s,
#           Ollama's own parallelism
#   after   answer queue = PARALLEL, Ollama OLLAMA_NUM_PARALLEL = PARALLEL,
#           gateway and answer timeouts 300 s, queue wait at most 600 s
# Reports go to /home/<user>/nexa-evidence. Settings are restored at the end.
# Restarting Ollama briefly interrupts anything else using it (e.g. v0).
set -uo pipefail
RUN_AS="${NEXA_EDGE_USER:-nvidia}"
PARALLEL="${PARALLEL:-2}"
SIZES_BEFORE="${SIZES_BEFORE:-10 30 100}"
SIZES_AFTER="${SIZES_AFTER:-10 30 100}"
OUT="/home/$RUN_AS/nexa-evidence"
APP=/opt/nexa-edge/app
PY=/opt/nexa-edge/venv/bin/python
KD=/etc/systemd/system/nexa-edge-knowledge.service.d
GD=/etc/systemd/system/nexa-edge-gateway.service.d
OD=/etc/systemd/system/ollama.service.d
mkdir -p "$OUT" "$KD" "$GD" "$OD"; chown "$RUN_AS": "$OUT"
if grep -qE '^(NEXA_GENERATION_CONCURRENCY|NEXA_GATEWAY_TIMEOUT|NEXA_ANSWER_TIMEOUT)=' /etc/nexa-edge/edge.env; then
  echo "edge.env sets the queue/timeouts itself (it overrides this test's settings)."
  echo "Comment those lines out in /etc/nexa-edge/edge.env, then run this again."; exit 2
fi
systemctl cat ollama >/dev/null 2>&1 || { echo "no ollama systemd service found; this test needs it"; exit 2; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

wait_health() {
  for _ in $(seq 1 60); do
    curl -sf -m 3 http://127.0.0.1:8103/health >/dev/null && curl -sf -m 3 http://127.0.0.1:8100/health >/dev/null && return 0
    sleep 2
  done
  log "services did not come up"; return 1
}

configure() {  # $1 = before | after
  rm -f "$KD/zz-loadtest.conf" "$GD/zz-loadtest.conf" "$OD/zz-loadtest.conf"
  if [ "$1" = after ]; then
    printf '[Service]\nEnvironment=NEXA_GENERATION_CONCURRENCY=%s\nEnvironment=NEXA_GENERATION_QUEUE_TIMEOUT_S=600\nEnvironment=NEXA_ANSWER_TIMEOUT=300\n' "$PARALLEL" > "$KD/zz-loadtest.conf"
    printf '[Service]\nEnvironment=NEXA_GATEWAY_TIMEOUT=300\n' > "$GD/zz-loadtest.conf"
    printf '[Service]\nEnvironment=OLLAMA_NUM_PARALLEL=%s\n' "$PARALLEL" > "$OD/zz-loadtest.conf"
  fi
  systemctl daemon-reload
  systemctl restart ollama; sleep 5
  systemctl restart nexa-edge-gateway
}

one_run() {  # $1 config, $2 students, $3 extra args, $4 label
  systemctl restart nexa-edge-knowledge
  wait_health || return 1
  log "run: $4"
  # The label goes in as an argument, never inside the quoted command, so any text is safe.
  sudo -u "$RUN_AS" bash -c 'set -a; . /etc/nexa-edge/edge.env; set +a; cd "$1" && "$2" v1/tools/nexa_load.py --students "$3" $4 --label "$5" --out "$6"' \
    _ "$APP" "$PY" "$2" "$3" "$4" "$OUT" \
    | grep -E 'Students who got|unavailable|HTTP errors|Wait, median|Whole class|Answers correct|Answer queue' \
    || log "RUN FAILED (no report): $4"
}

log "system: $(cat /proc/device-tree/model 2>/dev/null | tr -d '\0'), $(nproc) cores, $(free -g | awk '/Mem/{print $2}') GB"
log "ollama $(ollama --version 2>/dev/null | awk '{print $NF}'), environment: $(systemctl show ollama -p Environment --value)"
log "package: $(curl -s http://127.0.0.1:8103/health | $PY -c 'import sys,json;print(json.load(sys.stdin).get("edge"))')"

configure before
for n in $SIZES_BEFORE; do one_run before "$n" "" "before (current settings), $n students at once"; done
configure after
for n in $SIZES_AFTER; do one_run after "$n" "" "after (queue + parallel $PARALLEL), $n students at once"; done
one_run after 100 "--spread 300 --repeat-share 0.3" "after (queue + parallel $PARALLEL), 100 students over 5 min, 30% repeat a popular question"

log "restoring settings"
rm -f "$KD/zz-loadtest.conf" "$GD/zz-loadtest.conf" "$OD/zz-loadtest.conf"
systemctl daemon-reload; systemctl restart ollama; sleep 5; systemctl restart nexa-edge-gateway nexa-edge-knowledge
wait_health && log "done. Reports: ls -t $OUT/nexa-load-*.md"
