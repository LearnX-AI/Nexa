#!/usr/bin/env bash
# Tail the JSON logs of all v1 services from the single journald sink.
# Usage: sudo bash v1/deploy/nexa-logs.sh [extra journalctl args]
# Follow one trace end to end: sudo bash v1/deploy/nexa-logs.sh | grep <trace_id>
set -euo pipefail
exec journalctl \
  -u nexa-api-gateway.service \
  -u nexa-agent-runtime.service \
  -u nexa-model-gateway.service \
  -u nexa-identity.service \
  -u nexa-knowledge.service \
  -f -o cat "$@"
