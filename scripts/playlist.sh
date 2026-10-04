#!/usr/bin/env bash
# Whole-playlist imports, called by butler.sh. A playlist takes hours, so `start`
# runs it in the background and returns at once; `run` is that background loop: one
# importer call per PLAYLIST_BATCH (20) songs, a short report after each, to the
# PLAYLIST_NOTIFY command (e.g. hermes send) and to requests/spotify-<id>.log.
# `inbox` copies new CSVs from PLAYLIST_INBOX (e.g. where Hermes saves files sent
# to it on Telegram) into requests/. Settings: environment first, then .env (data).
#   PLAYLIST_NOTIFY    command that reads a report on stdin, split on spaces, e.g.
#                      hermes -p music-butler send --to telegram --quiet
#   PLAYLIST_INBOX     folder to pick new playlist CSVs up from
#   PLAYLIST_IMPORTER, PLAYLIST_SYNCER, PLAYLIST_CHECKER  importer commands (default:
#                      the downloader container; tests replace them)
set -Eeuo pipefail
IFS=$'\n\t'
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# setting KEY [default]: a variable set in the environment (even empty) wins, then
# the last KEY= line of .env; blank means the default.
setting() {
    local value line
    if [[ -n "${!1+set}" ]]; then
        value="${!1}"
    else
        line="$(grep -E "^$1=" .env 2>/dev/null | tail -n 1 || true)"
        value="${line#*=}"
    fi
    printf '%s' "${value:-${2:-}}"
}

valid_id() {
    [[ "${1:-}" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] || { echo 'invalid playlist id: use lowercase letters, digits, dashes' >&2; exit 64; }
}

running() {  # running <id>: the background import of this playlist is alive
    local pid
    pid="$(cat "requests/.playlist-$1.pid" 2>/dev/null || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

notify() {  # notify <id> <text>: log it, and send it if PLAYLIST_NOTIFY is set
    local command
    printf '%s %s\n\n' "$(date '+%F %T')" "$2" >> "requests/spotify-$1.log"
    command="$(setting PLAYLIST_NOTIFY)"
    [[ -n "$command" ]] || return 0
    IFS=' ' read -r -a cmd <<< "$command"
    printf '%s\n' "$2" | "${cmd[@]}" > /dev/null 2>&1 \
        || printf '%s notify failed\n' "$(date '+%F %T')" >> "requests/spotify-$1.log"
}

import_batch() {  # import_batch <id>: one batch; prints the importer's last line
    local importer
    importer="$(setting PLAYLIST_IMPORTER)"
    if [[ -n "$importer" ]]; then
        IFS=' ' read -r -a cmd <<< "$importer"
        "${cmd[@]}" "$1" < "requests/spotify-$1.csv" | tail -n 1
    else
        docker compose --progress quiet run --rm -T downloader --playlist "$1" < "requests/spotify-$1.csv" | tail -n 1
    fi
}

check_playlist() {  # check_playlist <id>: the importer's verdict on the CSV, before starting
    local checker
    checker="$(setting PLAYLIST_CHECKER)"
    if [[ -n "$checker" ]]; then
        IFS=' ' read -r -a cmd <<< "$checker"
        "${cmd[@]}" < "requests/spotify-$1.csv" | tail -n 1
    else
        docker compose --progress quiet run --rm -T downloader --playlist-check \
            < "requests/spotify-$1.csv" | tail -n 1
    fi
}

sync_playlist() {  # sync_playlist <id>: the user's Navidrome playlist of the same name
    local syncer name
    name="$(cat "requests/spotify-$1.name" 2>/dev/null || true)"
    name="${name:-$(printf '%s' "$1" | tr '-' ' ' | awk '{ for (i = 1; i <= NF; i++) $i = toupper(substr($i, 1, 1)) substr($i, 2) } 1')}"
    syncer="$(setting PLAYLIST_SYNCER)"
    if [[ -n "$syncer" ]]; then
        IFS=' ' read -r -a cmd <<< "$syncer"
        "${cmd[@]}" "$1" "$name" < "requests/spotify-$1.csv" | tail -n 1
    else
        docker compose --progress quiet run --rm -T downloader --playlist-sync "$1" --name "$name" \
            < "requests/spotify-$1.csv" | tail -n 1
    fi
}

sync_line() {  # sync_line <sync summary JSON>: one line for the final report
    python3 - "$1" <<'PY'
import json, sys
try:
    s = json.loads(sys.argv[1])
except ValueError:
    s = {}
if s.get("ok"):
    print('Tempo playlist "%s": %d songs.' % (s["playlist"], s["songs"]))
else:
    print("The Tempo playlist could not be updated: %s." % (s.get("reason") or "no answer"))
PY
}

# report <id> <summary JSON> <totals file>: prints "remaining<TAB>stop?" then the
# message; keeps running totals of what failed or had no certain match.
report() {
    python3 - "$1" "$2" "$3" <<'PY'
import json, sys
playlist, line, totals_path = sys.argv[1:]
try:
    totals = json.load(open(totals_path))
except (OSError, ValueError):
    totals = {"added": 0, "failed": [], "not_matched": []}
try:
    s = json.loads(line)
except ValueError:
    s = {}
if s.get("stopped") == "busy_retry_later":
    print("0\tbusy")  # another import holds the library: the caller waits and retries
    sys.exit()
if s.get("event") != "summary":
    print("0\tstop")
    print(f"Playlist {playlist}: stopped, the importer said {s.get('reason') or 'nothing'}"
          f" (code {s.get('code', '?')}). Start it again to continue.")
    sys.exit()
totals["added"] += len(s["added"])
totals["failed"] += [f"{f['song']} ({f.get('detail') or f['reason']})" for f in s["failed"]]
totals["not_matched"] += s["not_matched"]
json.dump(totals, open(totals_path, "w"))
done = s["remaining"] == 0
# Brief: counts only; songs are named only when they need the user (failed, no match).
lines = [f"Playlist {playlist}: +{len(s['added'])} imported, {s['in_library']} of {s['songs']} songs in the library"
         + (", import finished." if done else f", {s['remaining']} to go.")]
if s["failed"]:
    lines.append("Failed: " + "; ".join(f"{f['song']} ({f.get('detail') or f['reason']})" for f in s["failed"]))
if s["not_matched"]:
    lines.append("No certain match: " + "; ".join(s["not_matched"]))
if done and (totals["failed"] or totals["not_matched"]):
    lines.append(f"In total {len(totals['failed'])} failed and {len(totals['not_matched'])} had no certain "
                 "match: ask for them by name to pick a source.")
if s.get("stopped"):
    lines.append(f"Stopped: {s['stopped']}. Start the import again to continue.")
print(f"{s['remaining']}\t" + ("" if done or s.get("stopped") else "more"))
print("\n".join(lines))
PY
}

run() {
    local id="$1" out text head remaining busy=0 totals synced
    echo "$$" > "requests/.playlist-$id.pid"
    totals="requests/.playlist-$id.totals"
    rm -f "$totals"
    # Expanded now: the id is validated ([a-z0-9-]), and locals are gone at exit.
    trap "rm -f requests/.playlist-$id.pid requests/.playlist-$id.totals" EXIT
    while true; do
        out="$(import_batch "$id" || true)"
        text="$(report "$id" "$out" "$totals" || printf '0\tstop\nPlaylist %s: stopped, unreadable importer output.' "$id")"
        head="${text%%$'\n'*}"
        remaining="${head%%$'\t'*}"
        if [[ "$head" == *$'\t'busy ]]; then
            busy=$((busy + 1))  # another import holds the library (e.g. a scheduled job)
            if (( busy <= 30 )); then
                sleep "$(setting PLAYLIST_BUSY_WAIT 60)"
                continue
            fi
            text=$'0\tstop\n'"Playlist $id: stopped, the library stayed busy for 30 minutes. Start it again to continue."
        fi
        [[ "$head" == *$'\t'more && "$remaining" != 0 ]] || break
        notify "$id" "${text#*$'\n'}"
    done
    if [[ "$head" == *$'\t' ]]; then  # finished, not stopped
        # Wait for Navidrome to list the new songs, then (re)build the Tempo playlist.
        scripts/butler.sh playlists > /dev/null 2>&1 || true
        synced="$(sync_playlist "$id" 2>/dev/null || true)"
        text+=$'\n'"$(sync_line "$synced")"
    fi
    notify "$id" "${text#*$'\n'}"
}

start() {
    local id="$1" songs
    [[ -f "requests/spotify-$id.csv" && ! -L "requests/spotify-$id.csv" ]] || { echo "missing requests/spotify-$id.csv" >&2; exit 66; }
    if running "$id"; then
        printf 'Playlist %s is already being imported. Last report:\n%s\n' "$id" \
            "$(awk 'BEGIN { RS = "" } { last = $0 } END { print last }' "requests/spotify-$id.log" 2>/dev/null)"
        return 0
    fi
    # Checked here, not in the background, so a file that cannot be read is
    # reported in this command's answer instead of a message after it.
    songs="$(python3 -c 'import json, sys
try:
    s = json.loads(sys.argv[1])
except ValueError:
    s = {}
print(s["songs"] if s.get("ok") else "error\t%s" % (s.get("reason") or "no answer"))' "$(check_playlist "$id" || true)")"
    if [[ "$songs" == error* ]]; then
        printf 'Playlist %s was not started: the file cannot be read (%s). Export it again with Exportify, or send a CSV with track and artist columns.\n' \
            "$id" "${songs#error$'\t'}" >&2
        exit 65
    fi
    # A new session, so the import outlives the agent's or terminal's command.
    nohup python3 -c 'import os, sys
if os.fork(): sys.exit(0)
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])' scripts/playlist.sh run "$id" >> "requests/spotify-$id.log" 2>&1 < /dev/null &
    where="to the chat"
    [[ -n "$(setting PLAYLIST_NOTIFY)" ]] || where="in requests/spotify-$id.log"
    printf 'Started importing playlist %s (%s songs, those already in the library are skipped). A report comes %s every 20 songs.\n' \
        "$id" "$songs" "$where"
}

inbox() {
    local dir file name id base n known found=0
    dir="$(setting PLAYLIST_INBOX)"
    [[ -n "$dir" ]] || { echo 'PLAYLIST_INBOX is not set in .env' >&2; exit 64; }
    [[ -d "$dir" ]] || { echo "PLAYLIST_INBOX is not a folder: $dir" >&2; exit 66; }
    mkdir -p requests
    for file in "$dir"/*.csv "$dir"/*.CSV; do
        [[ -f "$file" && ! -L "$file" ]] || continue
        (( $(wc -c < "$file") <= 8388608 )) || continue
        for known in requests/spotify-*.csv; do
            [[ -f "$known" ]] && cmp -s "$file" "$known" && continue 2  # already picked up
        done
        name="${file##*/}"
        name="${name%.*}"
        # Hermes saves documents as doc_<random>_<original name>.
        [[ "$name" =~ ^doc_[0-9A-Za-z]{6,32}_(.+)$ ]] && name="${BASH_REMATCH[1]}"
        id="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | LC_ALL=C sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-40)"
        id="${id%-}"
        [[ -n "$id" ]] || id=playlist
        base="$id"
        n=2
        while [[ -e "requests/spotify-$id.csv" ]]; do
            id="$base-$n"
            n=$((n + 1))
        done
        cp -- "$file" "requests/spotify-$id.csv"
        # The file's own name, for the Tempo playlist: "My Summer Mix!", not my-summer-mix.
        printf '%s' "$(printf '%s' "$name" | LC_ALL=C tr -d '\000-\037' | cut -c1-100)" > "requests/spotify-$id.name"
        printf 'New playlist: %s (%s songs)\n' "$id" "$(( $(grep -c '' "requests/spotify-$id.csv") - 1 ))"
        found=1
    done
    (( found )) || echo 'No new playlist in the inbox.'
}

case "${1:-}" in
    start) valid_id "${2:-}"; start "$2" ;;
    run) valid_id "${2:-}"; run "$2" ;;
    inbox) inbox ;;
    *) echo "Usage: $0 start <id> | run <id> | inbox" >&2; exit 64 ;;
esac
