#!/usr/bin/env bash
# Songs YouTube's bot check held back ("Sign in to confirm you're not a bot"), called
# by butler.sh. The importer keeps them in <library>/discovery/deferred.json; `start`
# launches `run` in the background, which imports them again after a pause (default
# 1, 2, then 4 hours) until none are left, with a short report after each try to the
# PLAYLIST_NOTIFY command and to requests/retry.log. Settings: environment first,
# then .env (data).
#   RETRY_DELAYS      seconds to wait before each try, split on spaces
#   RETRY_IMPORTER    importer command (default: the downloader container; tests replace it)
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

deferred() {  # how many songs wait for a retry
    python3 -c 'import json, sys
try:
    print(len(json.load(open(sys.argv[1]))["songs"]))
except (OSError, ValueError, KeyError, TypeError):
    print(0)' "$(setting MUSIC_LIBRARY_PATH ./storage/library)/discovery/deferred.json"
}

running() {
    local pid
    pid="$(cat requests/.retry.pid 2>/dev/null || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

notify() {  # notify <text>: log it, and send it if PLAYLIST_NOTIFY is set
    local command
    printf '%s %s\n\n' "$(date '+%F %T')" "$1" >> requests/retry.log
    command="$(setting PLAYLIST_NOTIFY)"
    [[ -n "$command" ]] || return 0
    IFS=' ' read -r -a cmd <<< "$command"
    printf '%s\n' "$1" | "${cmd[@]}" > /dev/null 2>&1 \
        || printf '%s notify failed\n' "$(date '+%F %T')" >> requests/retry.log
}

import_deferred() {  # prints the importer's last line
    local importer
    importer="$(setting RETRY_IMPORTER)"
    if [[ -n "$importer" ]]; then
        IFS=' ' read -r -a cmd <<< "$importer"
        "${cmd[@]}" < /dev/null | tail -n 1
    else
        docker compose --progress quiet run --rm -T downloader --retry < /dev/null | tail -n 1
    fi
}

# report <summary JSON> <last try?>: one short message for the owner.
report() {
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    s = json.loads(sys.argv[1])
except ValueError:
    s = {}
last = sys.argv[2] == "1"
if s.get("event") != "summary":
    print(f"Retry of the songs YouTube blocked: the importer said {s.get('reason') or 'nothing'}.")
    sys.exit()
def songs(n):
    return f"{n} song" + ("" if n == 1 else "s")
def names(items):
    return "; ".join(f"{r['artist']} - {', '.join(r['titles'])}" for r in items)
lines = []
if s["added"]:
    lines.append(f"Added {songs(len(s['added']))} YouTube had blocked: {names(s['added'])}.")
if s["failed"]:
    lines.append("Failed: " + "; ".join(f"{f['artist']} - {', '.join(f['titles'])} ({f['reason']})" for f in s["failed"]))
if s.get("deferred"):
    lines.append(f"YouTube still blocks downloads; {songs(len(s['deferred']))} "
                 + ("waits" if len(s["deferred"]) == 1 else "wait")
                 + (" for the next add or scheduled run." if last else " for another try later."))
print("\n".join(lines) or "Nothing was waiting for a retry.")
PY
}

run() {
    local delays delay n=0 out
    echo "$$" > requests/.retry.pid
    trap 'rm -f requests/.retry.pid' EXIT
    IFS=' ' read -r -a delays <<< "$(setting RETRY_DELAYS '3600 7200 14400')"
    for delay in "${delays[@]}"; do
        n=$((n + 1))
        sleep "$delay"
        (( $(deferred) > 0 )) || return 0
        out="$(import_deferred || true)"
        notify "$(report "$out" "$(( n == ${#delays[@]} ))")"
        (( $(deferred) > 0 )) || return 0
    done
}

start() {
    (( $(deferred) > 0 )) || return 0
    running && return 0
    mkdir -p requests
    # A new session, so the retry outlives the agent's or terminal's command.
    nohup python3 -c 'import os, sys
if os.fork(): sys.exit(0)
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])' scripts/retry.sh run >> requests/retry.log 2>&1 < /dev/null &
}

case "${1:-}" in
    start) start ;;
    run) run ;;
    *) echo "Usage: $0 start | run" >&2; exit 64 ;;
esac
