#!/usr/bin/env bash
# Install this repository's skills (skills/*/SKILL.md) into a Hermes profile,
# rendered for Hermes: REPO becomes this checkout's absolute path (small models
# copy paths, they do not substitute placeholders) and a short note names the
# Hermes tools. Also writes the profile's cron scripts (music-audit.sh,
# music-discover.sh, music-discover-bootstrap.sh), which run scripts/scheduled.sh.
# Re-run after editing a SKILL.md.
set -Eeuo pipefail
IFS=$'\n\t'
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ $# -gt 1 ]]; then
    printf 'Usage: %s [hermes-profile]   (default: music-butler)\n' "$0" >&2
    exit 64
fi
profile="${1:-music-butler}"
[[ "$profile" =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]] || { echo 'invalid profile name' >&2; exit 64; }
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
if [[ "$profile" == default ]]; then
    profile_dir="$hermes_home"
else
    profile_dir="$hermes_home/profiles/$profile"
fi
[[ -d "$profile_dir" ]] || { echo "no Hermes profile at $profile_dir (hermes profile create $profile)" >&2; exit 66; }

repo="$PWD"
hermes_note="## Hermes tools

- Write request files with the \`write_file\` tool.
- Run \`butler.sh\` with the \`terminal\` tool, in the foreground. Do not set
  \`timeout\` or \`background\`: this profile already allows long imports.
- Paths below are absolute. Copy them exactly.
"
# bash 5.2 would expand & in replacements; 3.2 has no such option.
shopt -u patsub_replacement 2>/dev/null || true

for source in skills/*/SKILL.md; do
    name="${source#skills/}"
    name="${name%/SKILL.md}"
    skill="$(<"$source")"
    [[ "$skill" == *$'\n## '* ]] || { echo "$source has no '## ' section" >&2; exit 70; }
    skill="${skill//REPO/$repo}"
    # The note goes before the first section, after the skill's introduction.
    skill="${skill/$'\n## '/$'\n'$hermes_note$'\n## '}"
    target="$profile_dir/skills/media/$name"
    # Replace an earlier symlinked or rendered copy.
    [[ -L "$target" ]] && rm -- "$target"
    mkdir -p -- "$target"
    printf '%s\n' "$skill" > "$target/SKILL.md.tmp"
    mv -- "$target/SKILL.md.tmp" "$target/SKILL.md"
    printf 'Installed %s/SKILL.md for %s\n' "$target" "$repo"
done

# Hermes cron scripts take no arguments and must live in the profile's scripts/.
mkdir -p -- "$profile_dir/scripts"
for pair in music-audit:audit music-discover:weekly music-discover-bootstrap:bootstrap; do
    script="$profile_dir/scripts/${pair%%:*}.sh"
    printf '#!/usr/bin/env bash\n# Written by %s/scripts/install_hermes_skill.sh.\nexec %q %s\n' \
        "$repo" "$repo/scripts/scheduled.sh" "${pair#*:}" > "$script.tmp"
    chmod 755 "$script.tmp"
    mv -- "$script.tmp" "$script"
    printf 'Installed %s\n' "$script"
done
