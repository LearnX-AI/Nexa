#!/usr/bin/env bash
# NEXA v1 rollback (NEXA-M1-009).
#
# Usage:  NEXA_HOME=/home/admin/Nexa/Nexa bash v1/deploy/rollback.sh [git-sha]
#
# With no argument, returns to the deploy before the current one, read from
# .nexa_deploy_history. Rollback is a normal deploy of an older commit, so it
# gets the same health gate. Nothing is destroyed: the checkout is detached and
# environment files in /etc/nexa are not touched.
set -euo pipefail

NEXA_HOME="${NEXA_HOME:-/home/admin/Nexa/Nexa}"
HISTORY="$NEXA_HOME/.nexa_deploy_history"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  if [ ! -f "$HISTORY" ] || [ "$(wc -l < "$HISTORY")" -lt 2 ]; then
    echo "[nexa-rollback] no previous deploy recorded in $HISTORY; pass a commit sha" >&2
    exit 2
  fi
  TARGET="$(tail -n 2 "$HISTORY" | head -n 1)"
fi

echo "[nexa-rollback] rolling back to $TARGET"
exec bash "$HERE/deploy.sh" "$TARGET"
