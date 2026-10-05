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
  lookup <id>      search YouTube / read source metadata for requests/lookup-<id>.json
                   (downloads nothing)
  add <id>         validate requests/approved-<id>.json, then import it if valid
  validate <id>    check requests/approved-<id>.json offline (no download)
  import <id>      import requests/approved-<id>.json into the library
  status           Navidrome health and library contents
  library [page]   releases with id, tags, cover/lyrics status, 20 per page
                   (JSON; "pages" says how many; default page 1)
  retag <id>       fix tags/covers from requests/retag-<id>.json (no re-download)
  lyrics           retry synced lyrics (LRCLIB) for every song without them
  playlists        after imports: wait for the scan, refresh Discover Weekly etc.
  remove <id>      move releases in requests/remove-<id>.json to the library trash
  discover         songs to add next: playlist queue, radio candidates, songs
                   rated 1 star to remove (JSON; downloads nothing)
  seeds <id>       merge taste seeds from requests/seeds-<id>.json
  queue <id>       queue the songs of requests/spotify-<id>.csv (an Exportify export)
  playlist <id>    import all of requests/spotify-<id>.csv now, in the background,
                   20 songs at a time with a report after each (again: progress)
  inbox            copy new playlist CSVs from PLAYLIST_INBOX into requests/
  drives           list mounted external drives (macOS /Volumes, Linux /media)
  backup [drive]   encrypted backup to <drive mount>/music-butler-restic,
                   or to BACKUP_REPOSITORY from .env when no drive is given
<id> is 1-64 lowercase letters, digits and dashes, e.g. joplin-entertainer.
EOF
    exit 64
}

# request_file <kind> <id> [ext]: requests/<kind>-<id>.<ext> (default json), kind is
# approved, lookup, retag, remove, seeds or spotify.
request_file() {
    local id="${2:-}" ext="${3:-json}"
    # Small models often pass the file name: requests/lookup-songs.json means id "songs".
    id="${id#requests/}"
    id="${id#"$1"-}"
    id="${id%."$ext"}"
    # The error says what to do: small models otherwise guess, e.g. search words as arguments.
    local usage
    case "$1" in
        lookup) usage="Lookup takes one id, not search words: first write $PWD/requests/lookup-<id>.json containing {\"search\": [\"artist title\", ...]}, then run butler.sh lookup <id>." ;;
        spotify) usage="This command takes the id of $PWD/requests/spotify-<id>.csv; run butler.sh inbox first to pick up files the user sent." ;;
        *) usage="This command takes one id: first write $PWD/requests/$1-<id>.$ext as the music-butler skill says, then pass <id>." ;;
    esac
    usage="$usage The id uses lowercase letters, digits and dashes."
    # No dots or slashes, so an id can never leave requests/.
    [[ "$id" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] || { echo "invalid request id. $usage" >&2; exit 64; }
    local file="requests/$1-$id.$ext"
    [[ -f "$file" && ! -L "$file" ]] || { echo "missing $PWD/$file. $usage" >&2; exit 66; }
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
    lookup)
        file="$(request_file lookup "${2:-}")"
        "${compose[@]}" run --rm -T downloader --lookup < "$file"
        ;;
    add)
        # One step for agents: nothing is downloaded unless the whole request is valid.
        file="$(request_file approved "${2:-}")"
        "${compose[@]}" run --rm -T downloader --validate < "$file"
        "${compose[@]}" run --rm -T downloader < "$file"
        ;;
    validate)
        file="$(request_file approved "${2:-}")"
        "${compose[@]}" run --rm -T downloader --validate < "$file"
        ;;
    import)
        file="$(request_file approved "${2:-}")"
        "${compose[@]}" run --rm -T downloader < "$file"
        ;;
    library)
        page="${2:-1}"
        [[ "$page" =~ ^[1-9][0-9]{0,3}$ ]] || { echo 'invalid page: use a number, e.g. 2' >&2; exit 64; }
        "${compose[@]}" run --rm -T downloader --library "$page" < /dev/null
        ;;
    lyrics)
        "${compose[@]}" run --rm -T downloader --lyrics < /dev/null
        ;;
    playlists)
        "${compose[@]}" run --rm -T downloader --playlists < /dev/null
        ;;
    retag)
        file="$(request_file retag "${2:-}")"
        "${compose[@]}" run --rm -T downloader --retag < "$file"
        ;;
    remove)
        file="$(request_file remove "${2:-}")"
        "${compose[@]}" run --rm -T downloader --remove < "$file"
        ;;
    discover)
        "${compose[@]}" run --rm -T downloader --discover < /dev/null
        ;;
    seeds)
        file="$(request_file seeds "${2:-}")"
        "${compose[@]}" run --rm -T downloader --seeds < "$file"
        ;;
    queue)
        file="$(request_file spotify "${2:-}" csv)"
        "${compose[@]}" run --rm -T downloader --queue < "$file"
        ;;
    playlist)
        file="$(request_file spotify "${2:-}" csv)"
        id="${file#requests/spotify-}"
        scripts/playlist.sh start "${id%.csv}"
        ;;
    inbox)
        scripts/playlist.sh inbox
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
