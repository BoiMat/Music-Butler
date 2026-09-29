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
  drives           list mounted external drives (macOS /Volumes, Linux /media)
  backup [drive]   encrypted backup to <drive mount>/music-butler-restic,
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

# Where removable drives are mounted: macOS /Volumes; Linux desktop automounts
# (udisks2 uses /run/media/$USER or /media/$USER), then plain /media/<name>.
mount_roots() {
    if [[ "$(uname -s)" == Darwin ]]; then
        printf '%s\n' /Volumes
    else
        printf '%s\n' "/run/media/${USER:-$(id -un)}" "/media/${USER:-$(id -un)}" /media
    fi
}

# Prints "name<TAB>mount path" for each mounted drive; first root wins on duplicates.
drive_mounts() {
    local root volume name seen=$'\n'
    while IFS= read -r root; do
        [[ -d "$root" ]] || continue
        for volume in "$root"/*; do
            # The macOS boot volume appears in /Volumes as a symlink to /.
            [[ -d "$volume" && ! -L "$volume" ]] || continue
            name="${volume##*/}"
            # /media/<user> is a directory of mounts, not a drive itself.
            [[ "$volume" == "/media/${USER:-}" ]] && continue
            [[ "$(uname -s)" == Darwin ]] || mountpoint -q -- "$volume" 2>/dev/null || continue
            [[ "$seen" == *$'\n'"$name"$'\n'* ]] && continue
            seen+="$name"$'\n'
            printf '%s\t%s\n' "$name" "$volume"
        done
    done < <(mount_roots)
    return 0
}

drives() {
    drive_mounts | cut -f1
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
            mount_path="$(drive_mounts | awk -F '\t' -v d="$drive" '$1 == d && !found { print $2; found = 1 }')"
            [[ -n "$mount_path" ]] || { echo "drive not mounted: $drive" >&2; exit 75; }
            export RESTIC_REPOSITORY="$mount_path/music-butler-restic"
            [[ -f "$RESTIC_REPOSITORY/config" ]] || scripts/backup.sh init
        fi
        scripts/backup.sh run
        scripts/backup.sh restore-test
        ;;
    *)
        usage
        ;;
esac
