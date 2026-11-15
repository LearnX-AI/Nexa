#!/usr/bin/env bash
# Install or update the NEXA school edge services (NEXA-M2-009) on a school
# server. Run from the unpacked release directory (the one holding v1/):
#
#   sudo bash v1/edge/deploy/install.sh pomnsoe
#
# Leaves anything else on the machine alone (for example a v0 nexa.service on
# port 8000). Re-running updates the code and keeps the settings and packages.
set -euo pipefail
SCHOOL="${1:?usage: install.sh <school_id>}"
SRC="$(cd "$(dirname "$0")/../../.." && pwd)"
APP=/opt/nexa-edge/app
VENV=/opt/nexa-edge/venv
ENVF=/etc/nexa-edge/edge.env
HOME_DIR=/var/lib/nexa-edge
RUN_AS="${NEXA_EDGE_USER:-nvidia}"

echo "== code -> $APP"
mkdir -p "$APP" "$HOME_DIR" /etc/nexa-edge
rm -rf "$APP/v1.new" && cp -a "$SRC/v1" "$APP/v1.new" && rm -rf "$APP/v1" && mv "$APP/v1.new" "$APP/v1"
chown -R "$RUN_AS": "$HOME_DIR" /opt/nexa-edge

echo "== python environment"
if [ ! -x "$VENV/bin/python" ]; then sudo -u "$RUN_AS" python3 -m venv "$VENV"; fi
sudo -u "$RUN_AS" "$VENV/bin/pip" install -q --upgrade pip
sudo -u "$RUN_AS" "$VENV/bin/pip" install -q -r "$APP/v1/knowledge/requirements.txt" -r "$APP/v1/edge/requirements.txt" \
  -r "$APP/v1/model_gateway/requirements.txt" -r "$APP/v1/identity/requirements.txt"

if [ ! -f "$ENVF" ]; then
  echo "== settings $ENVF (new; secrets generated here, never printed)"
  SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
  umask 077
  cat > "$ENVF" <<CONF
NEXA_IDENTITY_SECRET=$SECRET
NEXA_KNOWLEDGE_STORE=edge
NEXA_EDGE_MODE=true
NEXA_EDGE_HOME=$HOME_DIR
NEXA_EDGE_SCHOOL_ID=$SCHOOL
NEXA_EDGE_ANSWER_ROUTE=local
# Answer queue: match Ollama's OLLAMA_NUM_PARALLEL (see loadtest.sh results)
NEXA_GENERATION_CONCURRENCY=2
NEXA_GENERATION_QUEUE_TIMEOUT_S=600
NEXA_ANSWER_TIMEOUT=300
NEXA_GATEWAY_TIMEOUT=300
NEXA_GENERAL_ANSWERS=llm
NEXA_KNOWLEDGE_REGISTRY=sqlite:///$HOME_DIR/registry.db
NEXA_GATEWAY_URL=http://127.0.0.1:8100
# From core NEXA: python -m v1.edge.cli school-key --school $SCHOOL --out <file>
NEXA_EDGE_SYNC_KEY=
# When this school can reach core NEXA (for example a named tunnel):
NEXA_EDGE_CORE_URL=
NEXA_EDGE_CORE_TOKEN=
CONF
  TOKEN=$(cd "$APP" && NEXA_IDENTITY_SECRET=$SECRET "$VENV/bin/python" -m v1.identity.cli \
          --subject "service:edge-knowledge-$SCHOOL" --roles service --ttl 315360000)
  echo "NEXA_KNOWLEDGE_GATEWAY_TOKEN=$TOKEN" >> "$ENVF"
  chown root:"$RUN_AS" "$ENVF"; chmod 640 "$ENVF"
fi

echo "== services"
install -m 644 "$APP/v1/edge/deploy/nexa-edge-gateway.service" /etc/systemd/system/
install -m 644 "$APP/v1/edge/deploy/nexa-edge-knowledge.service" /etc/systemd/system/
sed -i "s/^User=.*/User=$RUN_AS/" /etc/systemd/system/nexa-edge-*.service
systemctl daemon-reload
systemctl enable --now nexa-edge-gateway nexa-edge-knowledge
systemctl restart nexa-edge-gateway nexa-edge-knowledge
sleep 4
curl -s -m 5 http://127.0.0.1:8103/health && echo
echo "done. Next: set NEXA_EDGE_SYNC_KEY in $ENVF, then install a package:"
echo "  sudo -u $RUN_AS bash -c 'set -a; . $ENVF; set +a; cd $APP && $VENV/bin/python -m v1.edge.cli install <bundle.tar>'"
