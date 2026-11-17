#!/usr/bin/env bash
# NEXA M2 exit suite (NEXA-M2-010), core NEXA (DGX). Run as the service user:
#
#   bash ~/nexa-build/v1/deploy/m2_exit_suite.sh
#
# Runs, against the deployed services and the code in this checkout, every
# M2 check that produces evidence, each into its own report, and writes
# SUMMARY.md with the pass/fail line of each. It continues past a failure, so
# one failing check never hides the others. Asks for the sudo password once
# (to read /etc/nexa/*.env); nothing secret is printed or written.
#
#   1  services and LMS sync, access control, answers   nexa_live_check.py
#   2  LMS synchronisation (own demo site, cleaned up)  lms_sync_demo.py
#   3  ingestion, retrieval, grounding benchmark        nexa_eval.py corpus --answers (v0 baseline included)
#   4  live benchmark: admin, Pomnsoe student,          nexa_eval.py live
#      Pomnsoe student with course context
#   5  citation provenance to the original file         provenance_check.py
#   6  v0 knowledge inventory against v1                v0_inventory.py
set -uo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${NEXA_PY:-/home/admin/Nexa/venv-v1/bin/python}"
BASE="${NEXA_BASE:-http://127.0.0.1:8103}"
V0_DIR="${NEXA_V0_DIR:-/home/admin/Nexa/Nexa}"
SCHOOL="${NEXA_EXIT_SCHOOL:-pomnsoe}"
OUT="${NEXA_EXIT_OUT:-$HOME/nexa-evidence/m2-exit-$(date -u +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"
SUM="$OUT/SUMMARY.md"

set -a
# shellcheck disable=SC1090
. <(sudo cat /etc/nexa/identity.env /etc/nexa/knowledge.env)
set +a
cd "$HERE" || exit 2

{
  echo "# NEXA M2 exit suite, $(date -u +%FT%TZ)"
  echo
  echo "Code: \`$(git log --oneline -1)\`; deployed: \`$(tail -1 /home/admin/Nexa/Nexa/.nexa_deploy_history 2>/dev/null | cut -c1-12)\`; service: $BASE."
  echo
  echo "| # | Check | Result | Report |"
  echo "|---|---|---|---|"
} > "$SUM"

step() {  # $1 number, $2 name, $3 log file, rest: command
  local n="$1" name="$2" log="$3"; shift 3
  echo "[$(date -u +%H:%M:%S)] $n $name"
  "$@" > "$OUT/$log" 2>&1
  local rc=$?
  local line
  line="$(grep -m1 -E '^\*\*(Gate: )?(PASS|FAIL)|^\*\*(PASS|FAIL)|PASS|FAIL' "$OUT/$log" | tr -d '*' | cut -c1-120)"
  [ -z "$line" ] && line="exit code $rc"
  echo "| $n | $name | $([ $rc -eq 0 ] && echo pass || echo "**fail**") ($line) | $log |" >> "$SUM"
}

step 1 "Services, LMS sync state, access control, grounded answers" live-check.log \
  "$PY" v1/tools/nexa_live_check.py --out "$OUT"
step 2 "LMS synchronisation demonstration" lms-sync-demo.log \
  "$PY" v1/tools/lms_sync_demo.py --base "$BASE" --out "$OUT"
step 3 "Ingestion, retrieval and grounding benchmark (corpus, with v0 baseline)" eval-corpus.log \
  "$PY" v1/tools/nexa_eval.py corpus --corpus "$HOME/nexa-eval-corpus" --out "$OUT" --answers
step 4a "Live benchmark, platform admin" eval-live-admin.log \
  "$PY" v1/tools/nexa_eval.py live --base-url "$BASE" --out "$OUT"
step 4b "Live benchmark, $SCHOOL student" eval-live-student.log \
  "$PY" v1/tools/nexa_eval.py live --base-url "$BASE" --out "$OUT" --as-role student --school-id "$SCHOOL"
step 4c "Live benchmark, $SCHOOL student with course context" eval-live-student-context.log \
  "$PY" v1/tools/nexa_eval.py live --base-url "$BASE" --out "$OUT" --as-role student --school-id "$SCHOOL" --course-context
step 5 "Citation provenance to the original file" provenance.log \
  "$PY" v1/tools/provenance_check.py --base-url "$BASE" --out "$OUT"
step 6 "v0 knowledge inventory against v1" v0-inventory.log \
  "$PY" v1/tools/v0_inventory.py --v0-dir "$V0_DIR" --v1-url "$BASE" --out "$OUT"

echo >> "$SUM"
echo "Full reports (.md and .json) are in this folder." >> "$SUM"
echo "[$(date -u +%H:%M:%S)] done: $SUM"
cat "$SUM"
echo "ALL DONE"
