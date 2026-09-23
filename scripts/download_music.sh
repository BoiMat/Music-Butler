#!/usr/bin/env bash
# Deliberately tiny boundary: all untrusted request data is JSON, never shell code.
set -Eeuo pipefail
IFS=$'\n\t'
umask 027
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' '{"event":"error","code":69,"reason":"python3_required"}' >&2
    exit 69
fi
child=''
cleanup() {
    if [[ -n "$child" ]]; then
        kill -TERM "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Explicit redirection preserves stdin for the asynchronous child.
exec 3<&0
python3 "$SCRIPT_DIR/music_butler.py" "$@" <&3 &
child=$!
exec 3<&-
wait "$child"
child=''
