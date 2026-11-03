#!/usr/bin/env bash
# deploy_pythia.sh: roll mnemos-api on PYTHIA to a new image tag, with backup,
# health loop and automatic rollback. Procedure: docs/DEPLOY_PYTHIA.md.
#
# DRY-RUN BY DEFAULT: prints every mutating command and runs only read-only
# checks. Pass --apply to execute.
#
#   sudo scripts/deploy_pythia.sh --image localhost/mnemos-enterprise:<tag>            # dry run
#   sudo scripts/deploy_pythia.sh --image localhost/mnemos-enterprise:<tag> --announced --apply
#
# Options:
#   --image REF          new image (must already exist in root's podman storage)
#   --apply              execute (default: dry run)
#   --announced          confirm the restart window was announced on Hive topic
#                        orchestrator.coordination (required with --apply)
#   --skip-styx REASON   proceed when the pre-roll STYX backup fails (the data
#                        volume export is still mandatory); REASON is logged
#   --health-timeout S   seconds to wait for /health after the restart (default 180)
#
# Environment overrides (tests and non-standard hosts):
#   PODMAN SYSTEMCTL CURL       binaries (default podman, systemctl, curl)
#   MNEMOS_QUADLET              default /etc/containers/systemd/mnemos-api.container
#   MNEMOS_DEPLOY_STATE         default /var/lib/mnemos-deploy (backups + history)
#   MNEMOS_HEALTH_URL           default http://127.0.0.1:5002/health
#   MNEMOS_STYX_ENV             default /etc/mnemos/styx-r2.env
#   MNEMOS_HEALTH_INTERVAL      seconds between health polls (default 5)
#
# Exit codes: 0 deployed (or dry run ok), 1 usage/preflight, 2 backup failed,
# 3 new image unhealthy and rolled back, 4 rollback ALSO failed (page a human).
set -euo pipefail

PODMAN=${PODMAN:-podman}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
CURL=${CURL:-curl}
QUADLET=${MNEMOS_QUADLET:-/etc/containers/systemd/mnemos-api.container}
STATE=${MNEMOS_DEPLOY_STATE:-/var/lib/mnemos-deploy}
HEALTH_URL=${MNEMOS_HEALTH_URL:-http://127.0.0.1:5002/health}
STYX_ENV=${MNEMOS_STYX_ENV:-/etc/mnemos/styx-r2.env}
HEALTH_INTERVAL=${MNEMOS_HEALTH_INTERVAL:-5}
SERVICE=mnemos-api.service
CONTAINER=mnemos-api
VOLUME=mnemos-api-data

NEW_IMAGE=""
APPLY=0
ANNOUNCED=0
SKIP_STYX=""
HEALTH_TIMEOUT=180

log() { printf '%s deploy_pythia: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() {
	log "ERROR: $2"
	exit "$1"
}

# run CMD...: mutating step. Printed in dry run, executed with --apply.
run() {
	if [ "$APPLY" = 1 ]; then
		log "+ $*"
		"$@"
	else
		log "[dry-run] would run: $*"
	fi
}

while [ $# -gt 0 ]; do
	case "$1" in
	--image)
		NEW_IMAGE=${2:-}
		shift 2
		;;
	--apply)
		APPLY=1
		shift
		;;
	--announced)
		ANNOUNCED=1
		shift
		;;
	--skip-styx)
		SKIP_STYX=${2:-}
		[ -n "$SKIP_STYX" ] || die 1 "--skip-styx needs a reason"
		shift 2
		;;
	--health-timeout)
		HEALTH_TIMEOUT=${2:-}
		shift 2
		;;
	-h | --help)
		sed -n '2,32p' "$0"
		exit 0
		;;
	*) die 1 "unknown argument: $1" ;;
	esac
done

[ -n "$NEW_IMAGE" ] || die 1 "--image is required"
case "$HEALTH_TIMEOUT" in '' | *[!0-9]*) die 1 "--health-timeout must be a number of seconds" ;; esac
if [ "$APPLY" = 1 ] && [ "$ANNOUNCED" != 1 ]; then
	die 1 "announce the mnemos-api restart on Hive topic orchestrator.coordination first, then pass --announced"
fi

current_image() {
	sed -n 's/^[[:space:]]*Image=[[:space:]]*//p' "$QUADLET" | tail -n 1
}

# set_image REF: rewrite the quadlet's Image= line (atomic replace).
set_image() {
	local ref=$1 tmp
	if [ "$APPLY" != 1 ]; then
		log "[dry-run] would set Image=$ref in $QUADLET"
		return 0
	fi
	tmp=$(mktemp "${QUADLET}.XXXXXX")
	sed "s|^\([[:space:]]*\)Image=.*|\1Image=$ref|" "$QUADLET" >"$tmp"
	grep -q "^[[:space:]]*Image=$ref\$" "$tmp" || {
		rm -f "$tmp"
		die 4 "failed to rewrite Image= in $QUADLET"
	}
	chmod 0644 "$tmp"
	mv -f "$tmp" "$QUADLET"
	log "quadlet Image=$ref"
}

healthy_within() {
	local deadline=$(($(date +%s) + $1))
	while :; do
		if "$CURL" -fsS -m 5 "$HEALTH_URL" >/dev/null 2>&1; then
			return 0
		fi
		[ "$(date +%s)" -lt "$deadline" ] || return 1
		sleep "$HEALTH_INTERVAL"
	done
}

restart_on() {
	set_image "$1"
	run "$SYSTEMCTL" daemon-reload
	run "$SYSTEMCTL" restart "$SERVICE"
}

# ── preflight (read-only) ────────────────────────────────────────────────
[ -f "$QUADLET" ] || die 1 "quadlet not found: $QUADLET"
PREV_IMAGE=$(current_image)
[ -n "$PREV_IMAGE" ] || die 1 "no Image= line in $QUADLET"
[ "$PREV_IMAGE" != "$NEW_IMAGE" ] || die 1 "$NEW_IMAGE is already the deployed image"
"$PODMAN" image exists "$NEW_IMAGE" || die 1 "image not in local storage: $NEW_IMAGE (build or pull it first)"
"$PODMAN" image exists "$PREV_IMAGE" || die 1 "rollback image missing from local storage: $PREV_IMAGE"
"$CURL" -fsS -m 5 "$HEALTH_URL" >/dev/null 2>&1 || die 1 "current mnemos-api is not healthy at $HEALTH_URL; fix that before rolling"
log "current=$PREV_IMAGE new=$NEW_IMAGE apply=$APPLY"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="$STATE/$STAMP"

# ── backup ───────────────────────────────────────────────────────────────
run mkdir -p "$BACKUP_DIR"
run "$PODMAN" volume export "$VOLUME" --output "$BACKUP_DIR/$VOLUME.tar"
if [ "$APPLY" = 1 ]; then
	[ -s "$BACKUP_DIR/$VOLUME.tar" ] || die 2 "volume export produced no data at $BACKUP_DIR/$VOLUME.tar"
fi

# STYX from the NEW image against the still-running OLD API: proves the new
# image's backup client works and captures the Oracle corpus before restart.
styx_ok=1
if [ "$APPLY" = 1 ]; then
	if ! "$PODMAN" run --rm --network host --env-file "$STYX_ENV" "$NEW_IMAGE" python3 -m mnemos.tools.styx backup; then
		styx_ok=0
	fi
else
	log "[dry-run] would run: $PODMAN run --rm --network host --env-file $STYX_ENV $NEW_IMAGE python3 -m mnemos.tools.styx backup"
fi
if [ "$styx_ok" != 1 ]; then
	[ -n "$SKIP_STYX" ] || die 2 "pre-roll STYX backup failed; fix it or pass --skip-styx REASON"
	log "WARNING: STYX backup failed; proceeding on --skip-styx: $SKIP_STYX"
fi

# ── roll ─────────────────────────────────────────────────────────────────
if [ "$APPLY" = 1 ]; then
	printf '%s prev=%s new=%s backup=%s\n' "$STAMP" "$PREV_IMAGE" "$NEW_IMAGE" "$BACKUP_DIR" >>"$STATE/history.log"
fi
restart_on "$NEW_IMAGE"

if [ "$APPLY" != 1 ]; then
	log "[dry-run] would poll $HEALTH_URL for up to ${HEALTH_TIMEOUT}s, rolling back to $PREV_IMAGE on timeout"
	log "dry run complete; nothing changed"
	exit 0
fi

if healthy_within "$HEALTH_TIMEOUT"; then
	log "HEALTHY on $NEW_IMAGE (rollback image kept: $PREV_IMAGE; volume backup: $BACKUP_DIR)"
	exit 0
fi

# ── rollback ─────────────────────────────────────────────────────────────
log "new image not healthy after ${HEALTH_TIMEOUT}s; rolling back to $PREV_IMAGE"
"$PODMAN" logs --tail 80 "$CONTAINER" >"$BACKUP_DIR/failed-start.log" 2>&1 || true
restart_on "$PREV_IMAGE"
if healthy_within "$HEALTH_TIMEOUT"; then
	printf '%s ROLLED-BACK to %s\n' "$STAMP" "$PREV_IMAGE" >>"$STATE/history.log"
	die 3 "rolled back to $PREV_IMAGE (new image logs: $BACKUP_DIR/failed-start.log)"
fi
die 4 "ROLLBACK FAILED: $PREV_IMAGE is not healthy either; data volume backup at $BACKUP_DIR/$VOLUME.tar"
