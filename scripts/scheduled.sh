#!/usr/bin/env bash
# Scheduled agent runs: the weekly audit, the daily bootstrap and the weekly
# discovery. Run it from cron, a systemd timer or a Hermes no-agent cron job; its
# stdout is the run's report (empty = nothing to say). Settings come from the
# environment first, then .env (read as data, never as shell code):
#   SCHEDULE_AGENT     hermes | command
#   hermes:  HERMES_PROFILE (default music-butler), SCHEDULE_PROVIDER, SCHEDULE_MODEL,
#            SCHEDULE_MODEL_WEEKLY (default SCHEDULE_MODEL); blank = profile default;
#            HERMES_BIN (default hermes on PATH)
#   command: SCHEDULE_COMMAND, any agent CLI that reads the task on stdin and prints
#            its answer, e.g. claude -p --allowedTools Bash(scripts/butler.sh:*),Bash(./scripts/butler.sh:*),Read,Edit(requests/**)
#            (split on spaces, no shell quoting)
#   BOOTSTRAP_TARGET   library size that ends the bootstrap (default 150)
#   BUTLER_BIN         butler.sh to refresh playlists with (default scripts/butler.sh)
set -Eeuo pipefail
IFS=$'\n\t'
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

usage() {
    cat >&2 <<'EOF'
Usage: scripts/scheduled.sh <job>
  audit       fix library tags/covers/duplicates, then report   (weekly)
  bootstrap   add up to 25 songs, playlist queue first, until the library holds
              BOOTSTRAP_TARGET songs and the queue is empty; then report once
              and stay silent                                    (daily)
  weekly      remove stale discoveries, add 15 new songs, report (weekly)
EOF
    exit 64
}

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

[[ $# -eq 1 ]] || usage
job="$1"
repo="$PWD"
library="$(setting MUSIC_LIBRARY_PATH ./storage/library)"

case "$job" in
    audit)
        skill=music-audit
        model="$(setting SCHEDULE_MODEL)"
        task="Weekly music library audit. Follow the music-audit skill (not music-butler): read the library, fix what is certain (tags, covers, exact duplicates), then write a short report for the owner." ;;
    bootstrap)
        skill=music-discover
        model="$(setting SCHEDULE_MODEL)"
        target="$(setting BOOTSTRAP_TARGET 150)"
        [[ "$target" =~ ^[0-9]+$ ]] || { echo 'BOOTSTRAP_TARGET must be a number' >&2; exit 64; }
        songs="$(find "$library/music" -mindepth 2 -maxdepth 2 -type f \( -name '*.mp3' -o -name '*.flac' \) 2>/dev/null | wc -l | tr -d ' ')"
        queued="$(python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1]))))' \
            "$library/discovery/queue.json" 2>/dev/null || echo 0)"
        marker="$library/discovery/bootstrap.done"
        if (( songs >= target && queued == 0 )); then
            if [[ ! -e "$marker" ]]; then
                mkdir -p -- "$library/discovery"
                touch -- "$marker"
                printf 'Music bootstrap complete: %s songs in the library. Weekly discovery continues; the daily bootstrap job can now be removed.\n' "$songs"
            fi
            exit 0
        fi
        task="Music discovery in bootstrap mode: the library has $songs of $target songs and $queued playlist songs are queued. Follow the music-discover skill (not music-butler): add up to 25 songs, playlist queue first, then artist seeds, then candidates. Then write a short report for the owner." ;;
    weekly)
        skill=music-discover
        model="$(setting SCHEDULE_MODEL_WEEKLY "$(setting SCHEDULE_MODEL)")"
        task="Weekly music discovery. Follow the music-discover skill (not music-butler) in weekly mode: remove stale discoveries, add 15 new songs, then write a short report for the owner." ;;
    *) usage ;;
esac

agent="$(setting SCHEDULE_AGENT)"
status=0
case "$agent" in
    hermes)
        profile="$(setting HERMES_PROFILE music-butler)"
        provider="$(setting SCHEDULE_PROVIDER)"
        args=(-p "$profile" chat -Q -t terminal,file,skills -s "$skill")
        [[ -n "$provider" ]] && args+=(--provider "$provider")
        [[ -n "$model" ]] && args+=(-m "$model")
        out="$("$(setting HERMES_BIN hermes)" "${args[@]}" -q "$task" 2>&1)" || status=$?
        hermes_home="${HERMES_HOME:-$HOME/.hermes}"
        [[ "$profile" == default ]] && state="$hermes_home/state.db" || state="$hermes_home/profiles/$profile/state.db"
        # The report is the run's last assistant message in Hermes' session store:
        # filtering the terminal output once dropped a whole report.
        sid="$(printf '%s\n' "$out" | sed -n 's/^session_id: //p' | tail -n 1)"
        msg=""
        if [[ -n "$sid" ]]; then
            msg="$(python3 - "$state" "$sid" <<'PY' 2>/dev/null || true
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
row = db.execute("select content from messages where session_id = ? and role = 'assistant'"
                 " and trim(coalesce(content, '')) <> '' order by id desc limit 1", (sys.argv[2],)).fetchone()
print(row[0] if row else "")
PY
)"
        fi
        if [[ -n "$msg" ]]; then
            printf '%s\n' "$msg"
        else
            printf '%s\n' "$out" | grep -v -e '^session_id:' -e 'tirith security scanner' \
                | awk '/┊ review diff/ { skip = 1; next } skip && /^$/ { skip = 0; next } !skip' || true
        fi ;;
    command)
        command="$(setting SCHEDULE_COMMAND)"
        [[ -n "$command" ]] || { echo 'SCHEDULE_COMMAND is not set' >&2; exit 64; }
        IFS=' ' read -r -a cmd <<< "$command"
        prompt="$task
The skill is skills/$skill/SKILL.md in the current folder ($repo): read it first.
REPO in it means this folder: run butler.sh exactly as scripts/butler.sh, write request files to requests/."
        printf '%s\n' "$prompt" | "${cmd[@]}" || status=$? ;;
    '') echo 'SCHEDULE_AGENT is not set (hermes or command); see README "Scheduled runs"' >&2; exit 64 ;;
    *) echo "unknown SCHEDULE_AGENT: $agent (hermes or command)" >&2; exit 64 ;;
esac
# Navidrome re-evaluates the smart playlists (Discover Weekly...) only when their
# owner, the admin, opens them; the listener's Tempo would show stale songs. Its
# output is not part of the report.
"$(setting BUTLER_BIN scripts/butler.sh)" playlists > /dev/null 2>&1 || true
# Exit 0 either way: a failed job still delivers its report and this line.
(( status == 0 )) || printf 'Music %s run exited with status %s.\n' "$job" "$status"
