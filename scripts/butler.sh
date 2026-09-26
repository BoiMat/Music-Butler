#!/usr/bin/env bash
# Fixed entry point for AI agents. Arguments are an ID or a drive name, never
# free text: request data travels as a JSON file and nothing is interpolated
# into Docker or a shell.
set -Eeuo pipefail
IFS=$'\n\t'
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

usage() {
    cat >&2 <<'EOF'
Usage: scripts/butler.sh <command>
  validate <id>    check requests/approved-<id>.json offline (no download)
  import <id>      import requests/approved-<id>.json into the library
  status           Navidrome health and library contents
  drives           list mounted external drives (macOS)
  backup [drive]   encrypted backup to /Volumes/<drive>/music-butler-restic,
                   or to BACKUP_REPOSITORY from .env when no drive is given
<id> is 32 lowercase hex characters.
EOF
    exit 64
}

request_file() {
    [[ "${1:-}" =~ ^[0-9a-f]{32}$ ]] || { echo 'invalid request id' >&2; exit 64; }
    local file="requests/approved-$1.json"
    [[ -f "$file" && ! -L "$file" ]] || { echo "missing $file" >&2; exit 66; }
    printf '%s' "$file"
}

drives() {
    local volume
    for volume in /Volumes/*; do
        # The boot volume appears in /Volumes as a symlink to /.
        [[ -d "$volume" && ! -L "$volume" ]] && printf '%s\n' "${volume#/Volumes/}"
    done
    return 0
}

compose=(docker compose --progress quiet)

case "${1:-}" in
    validate)
        file="$(request_file "${2:-}")"
        "${compose[@]}" run --rm -T downloader --validate < "$file"
        ;;
    import)
        file="$(request_file "${2:-}")"
        "${compose[@]}" run --rm -T downloader < "$file"
        ;;
    status)
        "${compose[@]}" ps navidrome
        library="$(grep -E '^MUSIC_LIBRARY_PATH=' .env 2>/dev/null | tail -n 1 || true)"
        library="${library#*=}"
        printf 'Releases in library:\n'
        find "${library:-./storage/library}/music" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; 2>/dev/null \
            | sort | sed -E 's/--[0-9a-f]{32}$//; s/^/  /' || true
        ;;
    drives)
        drives
        ;;
    backup)
        if [[ -n "${2:-}" ]]; then
            drive="$2"
            [[ "$drive" =~ ^[A-Za-z0-9][A-Za-z0-9._\ -]{0,63}$ ]] || { echo 'invalid drive name' >&2; exit 64; }
            drives | grep -qxF -- "$drive" || { echo "drive not mounted: $drive" >&2; exit 75; }
            export RESTIC_REPOSITORY="/Volumes/$drive/music-butler-restic"
            [[ -f "$RESTIC_REPOSITORY/config" ]] || scripts/backup.sh init
        fi
        scripts/backup.sh run
        scripts/backup.sh restore-test
        ;;
    *)
        usage
        ;;
esac
