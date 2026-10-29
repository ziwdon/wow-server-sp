# shellcheck shell=bash
# Sourced by install-azerothcore-admin.sh and redeploy-azerothcore-admin.sh
# (`source tpm-env.sh`); never executed. Functions only: no output, no side
# effects and no `set`/`exit` at top level, so it is safe under the callers'
# `set -euo pipefail`.
#
# docker-compose.yml maps the host TPM into the admin container
# (`${TPM_DEVICE:-/dev/null}:/dev/tpmrm0`) and adds the `tss` group to the
# container user (`${TSS_GID:-1000}`) for the Cloud backup vault (spec 6.3).
# These helpers compute and persist those two .env values. A host without a TPM
# maps /dev/null, so one compose file works everywhere; whether a TPM really
# works is decided later by a functional probe inside the container.

# resolve_tpm_env: set TPM_DEVICE and TSS_GID in the caller's shell.
#   TPM_DEVICE  ${TPM_DEVICE_PATH:-/dev/tpmrm0} when that is a character
#               device, else /dev/null (never empty: compose would reject it).
#   TSS_GID     GID of the `tss` group, else $HOST_GID, else `id -g`. Any
#               numeric GID is accepted by Docker's group_add, so the
#               fallbacks cannot stop the container from starting.
# shellcheck disable=SC2034  # TPM_DEVICE/TSS_GID are this function's output, read by the sourcing script
resolve_tpm_env() {
    local device="${TPM_DEVICE_PATH:-/dev/tpmrm0}" gid=""
    if [ -c "$device" ]; then
        TPM_DEVICE="$device"
    else
        TPM_DEVICE=/dev/null
    fi

    # `|| true`: getent exits 2 when the group does not exist, which pipefail
    # would turn into a failed assignment and abort a `set -e` caller.
    gid="$(getent group tss 2>/dev/null | cut -d: -f3)" || true
    case "$gid" in '' | *[!0-9]*) gid="${HOST_GID:-}" ;; esac
    case "$gid" in '' | *[!0-9]*) gid="$(id -g)" ;; esac
    TSS_GID="$gid"
}

# backfill_tpm_env ENV_FILE: append TPM_DEVICE=/TSS_GID= to an existing admin
# .env, only for keys that are missing. Existing lines are never edited or
# reordered, and the file's mode is untouched (appending keeps it, 600 on a
# normal install). TPM_ENV_ADDED lists the keys it appended (empty = nothing
# to do). A missing/unwritable file is a warning and a non-zero return; the
# file is never created here, so a caller cannot make a 644 .env by accident.
# shellcheck disable=SC2034  # TPM_ENV_ADDED is this function's output, read by the sourcing script
backfill_tpm_env() {
    local env_file="${1:-}" key
    local -a missing=()
    TPM_ENV_ADDED=""
    if [ ! -f "$env_file" ] || [ ! -r "$env_file" ] || [ ! -w "$env_file" ]; then
        echo "WARNING: ${env_file:-<no file given>} is missing or not writable; TPM_DEVICE/TSS_GID not added." >&2
        return 1
    fi
    for key in TPM_DEVICE TSS_GID; do
        if ! grep -q "^${key}=" "$env_file"; then
            missing+=("$key")
        fi
    done
    if [ "${#missing[@]}" -eq 0 ]; then
        return 0
    fi

    resolve_tpm_env
    # Start on a fresh line when the last line has no newline (tail -c1 is
    # empty exactly when the file ends with one).
    if [ -s "$env_file" ] && [ -n "$(tail -c1 "$env_file")" ]; then
        printf '\n' >> "$env_file"
    fi
    for key in "${missing[@]}"; do
        case "$key" in
            TPM_DEVICE) printf 'TPM_DEVICE=%s\n' "$TPM_DEVICE" >> "$env_file" ;;
            TSS_GID) printf 'TSS_GID=%s\n' "$TSS_GID" >> "$env_file" ;;
        esac
    done
    TPM_ENV_ADDED="${missing[*]}"
}
