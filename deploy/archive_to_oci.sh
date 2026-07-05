#!/usr/bin/env bash
#
# archive_to_oci.sh — push a trading day's GammaLeak data to Oracle Object
# Storage as a single compressed tarball, so the calibration training set
# survives the instance being reaped/redeployed and can be pulled back from
# anywhere for offline training.
#
# Design choices:
#   * One object per trading day: daily/<date>.tar.gz  (atomic, ~5x smaller
#     than the raw CSVs, one fetch to reconstruct a whole session).
#   * Append-only: this script NEVER deletes local data. Retention/pruning of
#     the local copy is a separate, deliberate step (see DEPLOY.md).
#   * Idempotent: a per-day marker (logs/.archive_state/<date>.done) lets the
#     timer fire harmlessly and lets --catchup backfill any missed days.
#   * Uses rclone against Oracle's S3-compatible endpoint (remote name "oci"
#     by default). rclone verifies the upload via the object ETag.
#
# Usage:
#   archive_to_oci.sh                 # archive today (IST), then catch up misses
#   archive_to_oci.sh --date 2026-06-25
#   archive_to_oci.sh --catchup       # only backfill un-archived past days
#   archive_to_oci.sh --dry-run       # show what would happen, upload nothing
#
# Required env (set in the systemd unit or your shell):
#   GAMMALEAK_OCI_BUCKET   target bucket name (required)
# Optional env:
#   GAMMALEAK_HOME         repo root            (default: /opt/gammaleak)
#   GAMMALEAK_OCI_REMOTE   rclone remote name   (default: oci)
#   GAMMALEAK_OCI_PREFIX   key prefix in bucket (default: gammaleak)
#   RCLONE_CONFIG          path to rclone.conf  (default: rclone's own default)

set -euo pipefail

HOME_DIR="${GAMMALEAK_HOME:-/opt/gammaleak}"
LOG_DIR="${HOME_DIR}/logs"
STATE_DIR="${LOG_DIR}/.archive_state"
REMOTE="${GAMMALEAK_OCI_REMOTE:-oci}"
BUCKET="${GAMMALEAK_OCI_BUCKET:-}"
PREFIX="${GAMMALEAK_OCI_PREFIX:-gammaleak}"
ARCHIVE_LOG="${LOG_DIR}/archive.log"

DRY_RUN=0
CATCHUP_ONLY=0
ONE_DATE=""

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" | tee -a "$ARCHIVE_LOG" >&2; }
die() { log "ERROR: $*"; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --date)    ONE_DATE="${2:-}"; shift 2 ;;
        --catchup) CATCHUP_ONLY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *)         die "unknown argument: $1" ;;
    esac
done

command -v rclone >/dev/null 2>&1 || die "rclone not installed (see DEPLOY.md Step OS-3)"
[ -n "$BUCKET" ] || die "GAMMALEAK_OCI_BUCKET is not set"
[ -d "$LOG_DIR" ] || die "log dir not found: $LOG_DIR"
mkdir -p "$STATE_DIR"

REMOTE_BASE="${REMOTE}:${BUCKET}/${PREFIX}"
TODAY="$(date '+%F')"   # box is on Asia/Kolkata (DEPLOY.md Step 2), so this is IST
# Minutes-past-midnight after which today's session is considered closed and
# safe to archive (engine stops 15:35; default cutoff 15:40). Guards against a
# manual mid-session run shipping a partial day.
SESSION_CLOSE_HHMM="${GAMMALEAK_SESSION_CLOSE_HHMM:-1540}"

today_session_closed() {
    [ "$(date '+%H%M')" -ge "$SESSION_CLOSE_HHMM" ]
}

# Upload one date's data as daily/<date>.tar.gz. Returns 0 on success/skip.
archive_date() {
    local d="$1"
    local day_dir="${LOG_DIR}/${d}"
    local marker="${STATE_DIR}/${d}.done"

    if [ -f "$marker" ]; then
        log "[$d] already archived (marker present) — skipping"
        return 0
    fi
    if [ ! -d "$day_dir" ]; then
        log "[$d] no per-symbol log folder ${day_dir} — nothing to archive"
        return 0
    fi
    if [ "$d" = "$TODAY" ] && ! today_session_closed; then
        log "[$d] is today and session not yet closed (cutoff ${SESSION_CLOSE_HHMM} IST) — skipping to avoid a partial archive"
        return 0
    fi

    # Gather the day's artifacts relative to LOG_DIR so the tar reconstructs cleanly.
    local members=( "${d}" )
    [ -f "${LOG_DIR}/${d}_events.csv" ]   && members+=( "${d}_events.csv" )
    [ -f "${LOG_DIR}/${d}_oi_state.csv" ] && members+=( "${d}_oi_state.csv" )
    # Shadow research artifacts (2026-07: P2 per-strike dealer-gamma snapshots,
    # P4 expiry settlement-anchor track — expiry file exists only on Tuesdays)
    [ -f "${LOG_DIR}/${d}_gex.csv" ]      && members+=( "${d}_gex.csv" )
    [ -f "${LOG_DIR}/${d}_expiry.csv" ]   && members+=( "${d}_expiry.csv" )
    # Point-in-time copy of the cumulative IV history travels in the tarball too.
    [ -f "${LOG_DIR}/iv_session_history.csv" ] && members+=( "iv_session_history.csv" )

    local tarball="${LOG_DIR}/${d}.tar.gz"
    local dest="${REMOTE_BASE}/daily/${d}.tar.gz"

    log "[$d] packing ${#members[@]} artifact(s) -> ${tarball}"
    if [ "$DRY_RUN" -eq 1 ]; then
        log "[$d] DRY-RUN: would tar (${members[*]}) and rclone copyto -> ${dest}"
        return 0
    fi

    tar -czf "$tarball" -C "$LOG_DIR" "${members[@]}"
    local size; size="$(du -h "$tarball" | cut -f1)"
    log "[$d] uploading ${size} -> ${dest}"
    rclone copyto "$tarball" "$dest" --s3-no-check-bucket 2>>"$ARCHIVE_LOG"

    rm -f "$tarball"
    date '+%Y-%m-%dT%H:%M:%S%z' > "$marker"
    log "[$d] archived OK"
}

# Keep always-latest cumulative/learned state as standalone objects so training
# and a fresh redeploy can grab them without unpacking a daily tarball:
#   - iv_session_history.csv : rolling IV percentile baseline
#   - data/learned_gate.json : calibrate.py's learned regime gate (gitignored,
#     so this is its only durable home — restore it here after a redeploy)
sync_state() {
    local -a pairs=(
        "${LOG_DIR}/iv_session_history.csv|${REMOTE_BASE}/state/iv_session_history.csv"
        "${HOME_DIR}/data/learned_gate.json|${REMOTE_BASE}/state/learned_gate.json"
        # 3-year participant-OI backfill + daily upserts (positioning/anchor.py)
        # — gitignored data/, so this is its only durable home besides the box.
        "${HOME_DIR}/data/participant_history.csv|${REMOTE_BASE}/state/participant_history.csv"
    )
    local entry src dst
    for entry in "${pairs[@]}"; do
        src="${entry%%|*}"; dst="${entry##*|}"
        [ -f "$src" ] || continue
        if [ "$DRY_RUN" -eq 1 ]; then
            log "DRY-RUN: would refresh ${dst}"
            continue
        fi
        rclone copyto "$src" "$dst" --s3-no-check-bucket 2>>"$ARCHIVE_LOG"
        log "state: $(basename "$src") refreshed"
    done
}

main() {
    log "=== archive run start (remote=${REMOTE_BASE}, dry_run=${DRY_RUN}, catchup_only=${CATCHUP_ONLY}) ==="

    if [ -n "$ONE_DATE" ]; then
        archive_date "$ONE_DATE"
    else
        if [ "$CATCHUP_ONLY" -eq 0 ]; then
            # Timer fires 15:45 IST, post-close, so today's session is the one to ship.
            archive_date "$TODAY"
        fi
        # Backfill: any YYYY-MM-DD folder without a .done marker (today excluded inside archive_date).
        for path in "${LOG_DIR}"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]; do
            [ -d "$path" ] || continue
            archive_date "$(basename "$path")"
        done
    fi

    sync_state
    log "=== archive run done ==="
}

main
