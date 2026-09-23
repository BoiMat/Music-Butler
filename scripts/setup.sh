#!/usr/bin/env bash
# Provision only this clone's configured bind paths; never sudo/chown recursively.
set -Eeuo pipefail
IFS=$'\n\t'
umask 027
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != '--start' ) ]]; then
    printf 'Usage: %s [--start]\n' "$0" >&2
    exit 64
fi
command -v python3 >/dev/null
command -v docker >/dev/null
docker compose version >/dev/null
python3 - <<'PY'
import json, os, pathlib, subprocess
if os.getuid() == 0:
    raise SystemExit('Run setup as the unprivileged storage owner, not root.')
env = pathlib.Path('.env')
if not env.exists():
    value = pathlib.Path('.env.example').read_text().replace('LOCAL_UID=1000', f'LOCAL_UID={os.getuid()}').replace('LOCAL_GID=1000', f'LOCAL_GID={os.getgid()}')
    with env.open('x') as stream:
        stream.write(value)
    env.chmod(0o600)
config = json.loads(subprocess.check_output(['docker', 'compose', '--profile', 'tools', '--profile', 'hermes', 'config', '--format', 'json']))
expected = f'{os.getuid()}:{os.getgid()}'
for name, service in config['services'].items():
    if service['user'] != expected:
        raise SystemExit(f'{name}: LOCAL_UID/GID must match setup owner ({expected}); custom ownership needs admin provisioning.')
    for mount in service.get('volumes', []):
        if mount['type'] != 'bind':
            continue
        path = pathlib.Path(mount['source'])
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise SystemExit(f'Refusing symlink storage: {path}')
        path.mkdir(parents=True, exist_ok=True, mode=0o750)
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            raise SystemExit(f'Fix storage ownership before starting: {path}')
print('Configuration valid; storage provisioned; .env contains no bundled credentials.')
PY
if [[ "${1:-}" == '--start' ]]; then
    docker compose up -d
else
    printf '%s\n' 'Ready: docker compose up -d'
fi
