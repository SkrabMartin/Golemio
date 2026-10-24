#!/usr/bin/env bash
# Nightly backup of the Headway raw archive, plus a disk-usage check.
#
# Copies DATA_DIR/raw/ and DATA_DIR/gtfs/ to BACKUP_DEST with rsync. The archive is
# append-only, so the copy never deletes anything at the destination. Exits non-zero (and
# alerts via ntfy if NTFY_URL is set) when rsync fails or the data filesystem is fuller
# than DISK_ALERT_PCT.
set -euo pipefail

: "${BACKUP_DEST:?BACKUP_DEST must be set (see backup.env.example)}"
DATA_DIR="${DATA_DIR:-/srv/headway}"
DISK_ALERT_PCT="${DISK_ALERT_PCT:-85}"
NTFY_URL="${NTFY_URL:-}"

log() { printf '{"ts":"%s","level":"%s","msg":"%s"}\n' "$(date -u +%FT%TZ)" "$1" "$2"; }

notify() {
    if [[ -n "$NTFY_URL" ]]; then
        curl -fsS -m 10 -H "Title: headway: $1" -H "Priority: high" -d "$2" "$NTFY_URL" \
            >/dev/null || log warning "notification failed"
    fi
}

status=0

usage="$(df --output=pcent "$DATA_DIR" | tail -n 1 | tr -dc '0-9')"
size="$(du -sh "$DATA_DIR/raw" 2>/dev/null | cut -f1 || echo unknown)"
log info "disk usage ${usage}% on $DATA_DIR, raw archive ${size}"
if (( usage > DISK_ALERT_PCT )); then
    log warning "disk usage ${usage}% exceeds ${DISK_ALERT_PCT}%"
    notify "disk usage" "$(hostname): ${usage}% used on $DATA_DIR (raw archive ${size})"
    status=1
fi

for part in raw gtfs; do
    [[ -d "$DATA_DIR/$part" ]] || continue
    if rsync -a --partial --exclude '*.tmp' "$DATA_DIR/$part/" "$BACKUP_DEST/$part/"; then
        log info "backup of $part to $BACKUP_DEST finished"
    else
        code=$?
        log error "rsync of $part failed with exit code $code"
        notify "backup failed" "$(hostname): rsync of $part to $BACKUP_DEST failed (exit $code)"
        status=1
    fi
done

exit "$status"
