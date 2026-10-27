#!/bin/bash
set -euo pipefail

# ============================================================================
# migrate-ac-env.sh
# ----------------------------------------------------------------------------
# Migrate the AzerothCore Compose overlays across the mod-playerbots config
# rename (mod-playerbots PR #2854: AiPlayerbot.* -> Playerbots.*). Every
# AC_AI_PLAYERBOT_<X> env var becomes AC_PLAYERBOTS_<X>; the suffix never
# changed upstream, so the prefix swap is exact.
#
# Optionally pins extra AC_* values into docker-compose.admin.yml
# (--pin AC_KEY=VALUE) so an upstream default change cannot silently alter
# this server's behaviour.
#
# Files are rewritten IN PLACE (open + truncate). docker-compose.admin.yml is
# the bind-mount source of the admin container; rename(2) over it would leave
# the container on the old inode. Originals are copied to --backup-dir first.
#
# Usage:
#   migrate-ac-env.sh --stack-dir DIR --backup-dir DIR [--pin AC_K=V]... \
#                     [--validate-against FILE.conf.dist]... [--dry-run]
#   migrate-ac-env.sh --override FILE --admin FILE --backup-dir DIR ...
# Exit: 0 migrated (or nothing to do), 1 refused/failed, 2 usage.
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_VAR_PY="${ENV_VAR_PY:-${SCRIPT_DIR}/../wow-server-sp-admin/app/services/env_var.py}"

OVERRIDE=""
ADMIN=""
BACKUP_DIR=""
DRY_RUN=false
PINS=()
DISTS=()

usage() {
    sed -n '/^# Usage:/,/^# Exit:/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --stack-dir) OVERRIDE="${2:?}/docker-compose.override.yml"; ADMIN="${2:?}/docker-compose.admin.yml"; shift 2 ;;
        --override) OVERRIDE="${2:?}"; shift 2 ;;
        --admin) ADMIN="${2:?}"; shift 2 ;;
        --backup-dir) BACKUP_DIR="${2:?}"; shift 2 ;;
        --pin) PINS+=("${2:?}"); shift 2 ;;
        --validate-against) DISTS+=("${2:?}"); shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1" >&2; usage ;;
    esac
done

if [ -z "$OVERRIDE" ] || [ -z "$ADMIN" ]; then usage; fi
[ "$DRY_RUN" = true ] || [ -n "$BACKUP_DIR" ] || { echo "ERROR: --backup-dir is required unless --dry-run." >&2; exit 2; }
[ -f "$OVERRIDE" ] || { echo "ERROR: override file not found: $OVERRIDE" >&2; exit 1; }
for dist in ${DISTS[@]+"${DISTS[@]}"}; do
    [ -f "$dist" ] || { echo "ERROR: --validate-against file not found: $dist" >&2; exit 1; }
done

if [ "$DRY_RUN" != true ]; then
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    cp -p "$OVERRIDE" "$BACKUP_DIR/docker-compose.override.yml.pre-migrate"
    if [ -f "$ADMIN" ]; then
        cp -p "$ADMIN" "$BACKUP_DIR/docker-compose.admin.yml.pre-migrate"
    fi
fi

python3 -I - "$OVERRIDE" "$ADMIN" "$DRY_RUN" "$ENV_VAR_PY" \
    "${#PINS[@]}" ${PINS[@]+"${PINS[@]}"} ${DISTS[@]+"${DISTS[@]}"} <<'PY'
import difflib
import importlib.util
import re
import sys
from pathlib import Path

override, admin, dry_run, env_var_py, n_pins = sys.argv[1:6]
pins = sys.argv[6:6 + int(n_pins)]
dists = sys.argv[6 + int(n_pins):]
dry_run = dry_run == "true"

OLD, NEW = "AC_AI_PLAYERBOT_", "AC_PLAYERBOTS_"
ENV_LINE = re.compile(r"^(\s+)(AC_[A-Z0-9_]+):(.*)$")
PIN = re.compile(r"^(AC_[A-Z0-9_]+)=([A-Za-z0-9._,:-]*)$")
INDENT = "      "


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def env_keys(lines: list[str]) -> dict[str, str]:
    found = {}
    for line in lines:
        m = ENV_LINE.match(line)
        if m:
            found[m.group(2)] = m.group(3).strip().strip("'\"")
    return found


def rename(path: Path) -> tuple[list[str], int]:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    present = env_keys(lines)
    for key in present:
        if key.startswith(OLD) and NEW + key[len(OLD):] in present:
            fail(f"{path}: both {key} and {NEW + key[len(OLD):]} are set; resolve by hand")
    out, count = [], 0
    for line in lines:
        m = ENV_LINE.match(line)
        if m and m.group(2).startswith(OLD):
            line = f"{m.group(1)}{NEW}{m.group(2)[len(OLD):]}:{m.group(3)}" + ("\n" if line.endswith("\n") else "")
            count += 1
        out.append(line)
    return out, count


def apply_pins(path: Path, lines: list[str]) -> tuple[list[str], int]:
    if not pins:
        return lines, 0
    text = "".join(lines).strip()
    if text in ("", "services: {}"):
        lines = ["services:\n", "  ac-worldserver:\n", "    environment:\n"]
    if "    environment:\n" not in lines:
        fail(f"{path}: no ac-worldserver environment block; refusing to guess its layout")
    start = lines.index("    environment:\n")
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith(INDENT):
            fail(f"{path}: content follows the environment block; refusing to guess its layout")
    present = env_keys(lines)
    added = 0
    for pin in pins:
        m = PIN.match(pin)
        if not m:
            fail(f"invalid --pin {pin!r} (expected AC_KEY=VALUE with a shell-safe value)")
        key, value = m.groups()
        if key in present:
            if present[key] != value:
                fail(f"{path}: {key} is already '{present[key]}'; refusing to replace it with '{value}'")
            continue
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{INDENT}{key}: '{value}'\n")
        present[key] = value
        added += 1
    return lines, added


def validate(paths: list[Path], new_lines: dict[Path, list[str]]) -> None:
    if not dists:
        return
    spec = importlib.util.spec_from_file_location("env_var", env_var_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    known = set()
    key_line = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.-]*)\s*=")
    for dist in dists:
        for line in Path(dist).read_text(encoding="utf-8", errors="replace").splitlines():
            m = key_line.match(line)
            if m:
                known.add(module.config_key_to_ac_env_var(m.group(1)))
    pinned = {pin.split("=", 1)[0] for pin in pins}
    unknown = sorted(
        key for path in paths for key in env_keys(new_lines[path])
        if (key.startswith(NEW) or key in pinned)
        and key not in known and key != "AC_PLAYERBOTS_DATABASE_INFO"
    )
    if unknown:
        fail("no config key in the new .conf.dist files for: " + ", ".join(unknown))


targets = [Path(override)] + ([Path(admin)] if Path(admin).is_file() or pins else [])
new_lines: dict[Path, list[str]] = {}
for path in targets:
    if path.is_file():
        lines, renamed = rename(path)
    else:
        lines, renamed = [], 0
    pinned = 0
    if path == Path(admin):
        lines, pinned = apply_pins(path, lines)
    new_lines[path] = lines
    print(f"{path}: {renamed} renamed, {pinned} pinned")

validate(targets, new_lines)

for path in targets:
    old = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.is_file() else []
    new = new_lines[path]
    if old == new:
        continue
    if dry_run:
        sys.stdout.writelines(difflib.unified_diff(old, new, str(path), f"{path} (migrated)"))
        continue
    # In-place write: keeps the inode a bind mount may reference.
    with open(path, "a+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.truncate()
        handle.write("".join(new))
        handle.flush()
PY
