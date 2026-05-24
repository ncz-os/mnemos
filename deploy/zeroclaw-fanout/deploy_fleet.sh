#!/usr/bin/env bash
# Deploy canonical config + auto-fanout + code-exec shim to agent-pool hosts.
# Per-host User= override (mini for cixmini, ncz for Pis, jasonperlow elsewhere).
# Usage: ./deploy_fanout_fleet.sh

set -uo pipefail

REPO=/Users/jasonperlow/Projects/mnemos-prod-working/deploy/zeroclaw-fanout
CONFIG_TOML=/tmp/cixmini-zeroclaw-config.toml  # contains all real keys

if [ ! -f "$CONFIG_TOML" ]; then
  echo "FATAL: $CONFIG_TOML missing. Regenerate first."
  exit 1
fi

# Per-host roster: host_ip:user:home:max_n
ROSTER=(
  "192.168.207.96:jasonperlow:/home/jasonperlow:4"     # CERBERUS GPU contention cap
  "192.168.207.64:jasonperlow:/home/jasonperlow:6"     # MEDUSA
  "192.168.207.67:jasonperlow:/home/jasonperlow:6"     # PYTHIA
  "192.168.207.8:jasonperlow:/home/jasonperlow:4"      # HYDRA
  "192.168.207.65:ncz:/home/ncz:2"                     # bigpi
  "192.168.207.54:ncz:/home/ncz:2"                     # clawpi
)

deploy_one() {
  local host=$1 user=$2 home=$3 max_n=$4
  local sudo_pw=""
  case "$user" in
    mini) sudo_pw=mini ;;
    ncz) sudo_pw='***REMOVED-CREDENTIAL***' ;;
    jasonperlow) sudo_pw="" ;;  # sudo -n (passwordless via NOPASSWD)
  esac

  echo "=== $user@$host (home=$home, max_n=$max_n) ==="

  # Stage on host
  ssh -o ConnectTimeout=10 "$user@$host" "mkdir -p ~/staging" 2>&1 | tail -3
  scp -q "$CONFIG_TOML" "$user@$host:~/staging/config.toml"
  scp -q "$REPO/zeroclaw_worker.py" "$user@$host:~/staging/zeroclaw_worker.py"
  scp -q "$REPO/zeroclaw-fanout-init.sh" "$user@$host:~/staging/"
  scp -q "$REPO/zeroclaw-fanout.service" "$user@$host:~/staging/"

  # Build per-host systemd unit
  cat > /tmp/zc-unit-$host <<UNIT
[Unit]
Description=Zeroclaw Hive Worker instance %i
After=network.target

[Service]
Type=simple
User=$user
EnvironmentFile=-/etc/default/zeroclaw-worker
Environment=ZEROCLAW_INSTANCE_ID=%i
Environment=INSTANCE=%i
ExecStart=/usr/bin/python3 $home/zeroclaw_worker.py
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
  scp -q /tmp/zc-unit-$host "$user@$host:~/staging/zeroclaw-worker@.service"

  # Build per-host fanout env override (cap N)
  echo "ZC_FANOUT_FORCE_N=$max_n" > /tmp/zc-fanout-env-$host
  scp -q /tmp/zc-fanout-env-$host "$user@$host:~/staging/zeroclaw-fanout-env"

  # Install + restart
  if [ -n "$sudo_pw" ]; then
    ssh "$user@$host" "echo '$sudo_pw' | sudo -S bash -c '
      install -m 0640 -o $user -g $user ~$user/staging/config.toml $home/.zeroclaw/config.toml
      install -m 0755 -o $user -g $user ~$user/staging/zeroclaw_worker.py $home/zeroclaw_worker.py
      install -m 0755 ~$user/staging/zeroclaw-fanout-init.sh /usr/local/sbin/zeroclaw-fanout-init
      install -m 0644 ~$user/staging/zeroclaw-fanout.service /etc/systemd/system/zeroclaw-fanout.service
      install -m 0644 ~$user/staging/zeroclaw-worker@.service /etc/systemd/system/zeroclaw-worker@.service
      install -m 0644 ~$user/staging/zeroclaw-fanout-env /etc/default/zeroclaw-fanout
      systemctl daemon-reload
      systemctl stop zeroclaw-worker@1 2>/dev/null || true
      systemctl reset-failed zeroclaw-fanout zeroclaw-worker@1 2>/dev/null || true
      systemctl enable --now zeroclaw-fanout.service
    '" 2>&1 | tail -4
  else
    ssh "$user@$host" "sudo -n bash -c '
      install -m 0640 -o $user -g $user ~$user/staging/config.toml $home/.zeroclaw/config.toml
      install -m 0755 -o $user -g $user ~$user/staging/zeroclaw_worker.py $home/zeroclaw_worker.py
      install -m 0755 ~$user/staging/zeroclaw-fanout-init.sh /usr/local/sbin/zeroclaw-fanout-init
      install -m 0644 ~$user/staging/zeroclaw-fanout.service /etc/systemd/system/zeroclaw-fanout.service
      install -m 0644 ~$user/staging/zeroclaw-worker@.service /etc/systemd/system/zeroclaw-worker@.service
      install -m 0644 ~$user/staging/zeroclaw-fanout-env /etc/default/zeroclaw-fanout
      systemctl daemon-reload
      systemctl stop zeroclaw-worker@1 2>/dev/null || true
      systemctl reset-failed zeroclaw-fanout zeroclaw-worker@1 2>/dev/null || true
      systemctl enable --now zeroclaw-fanout.service
    '" 2>&1 | tail -4
  fi

  # Verify
  ssh "$user@$host" "systemctl list-units 'zeroclaw-worker@*' --no-pager 2>&1 | head -10" 2>&1 | grep "loaded" | wc -l | xargs echo "  active workers:"
  rm -f /tmp/zc-unit-$host /tmp/zc-fanout-env-$host
  echo ""
}

for entry in "${ROSTER[@]}"; do
  IFS=':' read -r host user home max_n <<< "$entry"
  deploy_one "$host" "$user" "$home" "$max_n"
done

echo "=== fleet deploy complete ==="
