#!/usr/bin/env bash
# Offline validation; container builds/smoke tests are explicit separate actions.
set -Eeuo pipefail
IFS=$'\n\t'
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ -x .venv/bin/python ]]; then
    export PATH="$PWD/.venv/bin:$PATH"
fi
bash -n scripts/download_music.sh scripts/setup.sh scripts/validate.sh
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck scripts/*.sh
else
    printf '%s\n' 'SKIP: shellcheck not installed' >&2
fi
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -v
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose --env-file .env.example --profile tools --profile hermes config --quiet
else
    printf '%s\n' 'SKIP: Docker Compose not installed' >&2
fi
