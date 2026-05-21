#!/usr/bin/env bash
# Stand up MNEMOS-on-Oracle on PROTEUS (192.168.207.25).
#
# Prereqs:
#   - feat/oracle-port already cloned at /opt/mnemos-oracle
#   - /etc/mnemos/mnemos.env carries the Oracle DSN + bearer + keys
#     (see scripts/oracle_proteus_env_stage.py for the stage step)
#   - Oracle 23ai Free running on 127.0.0.1:1521/FREEPDB1
#
# Idempotent: re-run after a code update to refresh venv + restart.
set -euo pipefail

REPO=/opt/mnemos-oracle
VENV="$REPO/venv"
PYBIN=python3
SERVICE_USER=mnemos

if [[ ! -d $REPO ]]; then
  echo "ERROR: $REPO missing. Clone feat/oracle-port first." >&2
  exit 1
fi

cd "$REPO"
git fetch && git checkout feat/oracle-port && git pull origin feat/oracle-port

if [[ ! -d $VENV ]]; then
  echo "[venv] creating $VENV"
  $PYBIN -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip wheel
"$VENV/bin/pip" install \
  asyncpg \
  oracledb \
  fastapi \
  uvicorn \
  httpx \
  pydantic \
  pydantic-settings \
  redis \
  tomli \
  python-multipart \
  jinja2

# Make sure the service user can read the venv + the working tree.
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO" || sudo chown -R "$USER:$USER" "$REPO"

# Drop a systemd override that swaps WorkingDirectory and the entry
# point onto the Oracle checkout. The existing mnemos.service unit
# keeps the EnvironmentFile=/etc/mnemos/mnemos.env line untouched.
sudo mkdir -p /etc/systemd/system/mnemos.service.d
sudo tee /etc/systemd/system/mnemos.service.d/oracle-override.conf >/dev/null <<EOF
[Service]
WorkingDirectory=$REPO
ExecStart=
ExecStart=$VENV/bin/python $REPO/api_server.py
EOF

sudo systemctl daemon-reload
sudo systemctl restart mnemos.service
sleep 3
sudo systemctl status mnemos.service --no-pager -l | head -20
echo
echo "[health] http://127.0.0.1:5002/health"
curl -s -o /dev/null -w 'status=%{http_code} time=%{time_total}s\n' http://127.0.0.1:5002/health || true
