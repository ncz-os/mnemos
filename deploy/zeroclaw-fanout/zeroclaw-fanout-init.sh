#!/usr/bin/env bash
# zeroclaw fan-out bootstrap
# Probes cores+RAM, computes N=min(cores//2, ram_gb//2, 8), enables
# zeroclaw-worker@1..@N systemd instances.
# Idempotent — safe to re-run on every boot.
# Cap: max 8 instances per host to avoid stampeding hive.

set -uo pipefail

LOG_TAG="zc-fanout"

log() { logger -t "$LOG_TAG" -s "$*"; }

# Honor explicit override env (set in /etc/default/zeroclaw-fanout)
if [ -f /etc/default/zeroclaw-fanout ]; then
  # shellcheck disable=SC1091
  . /etc/default/zeroclaw-fanout
fi

if [ -n "${ZC_FANOUT_FORCE_N:-}" ]; then
  N="$ZC_FANOUT_FORCE_N"
  log "fan-out forced by env: N=$N"
else
  # Probe
  CORES=$(nproc 2>/dev/null || echo 1)
  RAM_KB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 1048576)
  RAM_GB=$(( RAM_KB / 1024 / 1024 ))
  [ "$RAM_GB" -lt 1 ] && RAM_GB=1

  # N = min(cores/2, ram_gb/2, 8) ; floor 1
  N_CORES=$(( CORES / 2 ))
  N_RAM=$(( RAM_GB / 2 ))
  [ "$N_CORES" -lt 1 ] && N_CORES=1
  [ "$N_RAM" -lt 1 ] && N_RAM=1
  N=$N_CORES
  [ "$N_RAM" -lt "$N" ] && N=$N_RAM
  [ "$N" -gt 8 ] && N=8
  log "probe: cores=$CORES ram_gb=$RAM_GB => N=$N"
fi

# Find currently-enabled @* instances
ENABLED=$(systemctl list-unit-files 'zeroclaw-worker@*.service' --state=enabled --no-legend 2>/dev/null | awk '{print $1}' | sed -nE 's/zeroclaw-worker@([0-9]+)\.service/\1/p' | sort -n)
log "currently enabled: ${ENABLED:-none}"

# Enable @1..@N
for i in $(seq 1 "$N"); do
  if ! systemctl is-enabled --quiet "zeroclaw-worker@$i" 2>/dev/null; then
    systemctl enable "zeroclaw-worker@$i" 2>/dev/null || log "WARN: enable @$i failed"
    log "enabled @$i"
  fi
  systemctl restart --no-block "zeroclaw-worker@$i" || log "WARN: restart @$i failed"
done

# Disable instances above N (down-scaling)
for instance in $ENABLED; do
  if [ "$instance" -gt "$N" ]; then
    systemctl disable --now "zeroclaw-worker@$instance" 2>/dev/null || true
    log "disabled @$instance (above cap)"
  fi
done

log "fan-out complete: N=$N active"
