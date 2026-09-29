#!/usr/bin/env bash
# Encrypted restic backup of the library, a consistent Navidrome DB copy and local config.
# Usage: scripts/backup.sh [init|run|snapshots|restore-test]   (default: run)
set -Eeuo pipefail
IFS=$'\n\t'
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
# launchd starts with a minimal PATH; Docker Desktop and Homebrew live here.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"; }

# Read single keys from .env as data; it is Compose config, never shell code.
env_value() {
    local line
    line="$(grep -E "^$1=" .env 2>/dev/null | tail -n 1 || true)"
    printf '%s' "${line#*=}"
}

export RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-$(env_value BACKUP_REPOSITORY)}"
if [[ -z "$RESTIC_REPOSITORY" ]]; then
    log 'BACKUP_REPOSITORY is not set in .env' >&2
    exit 64
fi
if [[ -z "${RESTIC_PASSWORD_FILE:-}${RESTIC_PASSWORD_COMMAND:-}" ]]; then
    if ! command -v security >/dev/null 2>&1; then
        log 'Set RESTIC_PASSWORD_FILE (no macOS Keychain on this host)' >&2
        exit 64
    fi
    export RESTIC_PASSWORD_COMMAND='security find-generic-password -a music-butler -s music-butler-restic -w'
fi
command -v restic >/dev/null || { log 'restic is not installed' >&2; exit 69; }

library="$(env_value MUSIC_LIBRARY_PATH)"; library="${library:-./storage/library}"
data="$(env_value NAVIDROME_DATA_PATH)"; data="${data:-./storage/navidrome}"
secrets_dir="$(env_value SECRETS_PATH)"; secrets_dir="${secrets_dir:-./secrets}"
abs() { (cd -- "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
library="$(abs "$library")"; data="$(abs "$data")"; secrets_dir="$(abs "$secrets_dir")"

# A local repository on an unplugged drive is a normal skip, not a failure.
repo_available() {
    [[ "$RESTIC_REPOSITORY" != /* ]] || [[ -d "$(dirname -- "$RESTIC_REPOSITORY")" ]]
}

database_copy() {
    if docker compose ps --status running --services 2>/dev/null | grep -qx navidrome; then
        docker compose exec -T navidrome /app/navidrome backup create --nobanner >/dev/null 2>&1
    elif [[ ! -f "$data/navidrome.db" ]]; then
        # Navidrome has never started here: there is no database to copy yet.
        log "No Navidrome database at $data/navidrome.db yet; backing up the library only"
    else
        # Stopped server: the database is quiescent, so a plain copy is consistent.
        mkdir -p "$data/backup"
        cp -p "$data/navidrome.db" "$data/backup/navidrome_backup_stopped_$(date '+%Y.%m.%d_%H.%M.%S').db"
    fi
}

case "${1:-run}" in
    init)
        repo_available || { log "Destination not mounted: $RESTIC_REPOSITORY" >&2; exit 75; }
        restic init
        ;;
    run)
        if ! repo_available; then
            log "Destination not mounted, skipping: $RESTIC_REPOSITORY"
            exit 0
        fi
        log 'Creating consistent Navidrome database copy'
        database_copy
        # restic fails on missing paths, so add optional ones only when present.
        paths=("$library")
        for optional in "$data/backup" "$secrets_dir" "$PWD/.env" "$PWD/requests"; do
            [[ -e "$optional" ]] && paths+=("$optional")
        done
        log 'Backing up'
        restic backup --tag music-butler --one-file-system \
            --exclude "$library/.staging" --exclude "$library/.ingest.lock" \
            "${paths[@]}"
        log 'Pruning old snapshots'
        restic forget --tag music-butler --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune
        log 'Verifying repository'
        restic check --read-data-subset=5%
        log 'Backup complete'
        ;;
    snapshots)
        restic snapshots
        ;;
    restore-test)
        # Restore the latest snapshot's music tree to a scratch dir and compare every file hash.
        # RESTORE_TEST_DIR can point at a disk with room for a full copy of the library.
        scratch="${RESTORE_TEST_DIR:-${TMPDIR:-/tmp}}"
        need_kb="$(du -sk "$library/music" | cut -f1)"
        free_kb="$(df -Pk "$scratch" | awk 'NR == 2 { print $4 }')"
        if (( need_kb + 1048576 > free_kb )); then
            log "Not enough space in $scratch for a restore test (${need_kb} KiB + 1 GiB headroom needed); set RESTORE_TEST_DIR" >&2
            exit 73
        fi
        target="$(mktemp -d "$scratch/music-butler-restore.XXXXXX")"
        trap 'rm -rf "$target"' EXIT
        restic restore latest --target "$target" --include "$library/music"
        restored="$target$library"
        if command -v sha256sum >/dev/null 2>&1; then
            sha=(sha256sum)
        else
            sha=(shasum -a 256)
        fi
        (cd "$library/music" && find . -type f ! -name '.*' -print0 | sort -z | xargs -0 "${sha[@]}") > "$target/live.sha"
        (cd "$restored/music" && find . -type f ! -name '.*' -print0 | sort -z | xargs -0 "${sha[@]}") > "$target/restored.sha"
        diff "$target/live.sha" "$target/restored.sha"
        log "Restore test passed: $(wc -l < "$target/live.sha" | tr -d ' ') files identical"
        ;;
    *)
        printf 'Usage: %s [init|run|snapshots|restore-test]\n' "$0" >&2
        exit 64
        ;;
esac
