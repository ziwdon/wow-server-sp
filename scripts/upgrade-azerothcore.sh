#!/bin/bash
set -euo pipefail

# ============================================================================
# upgrade-azerothcore.sh
# ----------------------------------------------------------------------------
# Upgrade the AzerothCore core + modules to the revisions pinned in
# scripts/upstream-versions.env, with a tested rollback path.
#
#   preflight  read-only go/no-go checks against the live stack
#   stage      staging checkout + candidate images + client data (live keeps running)
#   rehearse   boot the candidate against a restored backup on an isolated network
#   cutover    the only disruptive step: cold backup, switch, boot, verify
#   rollback   return to the exact pre-cutover state (code, images, configs, DB);
#              after go-live it first saves the upgraded state (prerollback backup)
#   finalize   delete rollback artifacts after the upgrade has proven itself
#   status     show the current upgrade state
#
# Usage:
#   upgrade-azerothcore.sh preflight --previous-repo-rev REV
#   upgrade-azerothcore.sh stage
#   upgrade-azerothcore.sh rehearse [--archive FILE] [--pin AC_K=V]...   (the cutover's --pin set)
#   upgrade-azerothcore.sh cutover --previous-repo-rev REV [--pin AC_K=V]... [--yes]
#                                  [--state DIR] [--skip-rehearsal] [--allow-online-players]
#   upgrade-azerothcore.sh rollback [--state DIR] [--yes] [--no-safety-backup]
#   upgrade-azerothcore.sh rollback [--state DIR] --accept-verify-failure
#   upgrade-azerothcore.sh finalize [--state DIR] [--yes]
#   upgrade-azerothcore.sh status
#   Add --dry-run to print every mutating command instead of running it.
#
# Work root: $UPGRADE_ROOT (default ~/azerothcore-upgrade), mode 700, outside
# /opt/stacks so backup pruning and stack teardown never touch it.
# ============================================================================

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: do not run as root; docker is invoked as your user (docker group)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STACK_DIR="${STACK_DIR:-/opt/stacks/azerothcore}"
ADMIN_STACK_DIR="${ADMIN_STACK_DIR:-/opt/stacks/azerothcore-admin}"
UPGRADE_ROOT="${UPGRADE_ROOT:-${HOME}/azerothcore-upgrade}"
VERSIONS_FILE="${VERSIONS_FILE:-${SCRIPT_DIR}/upstream-versions.env}"
STOP_TIMEOUT="${STOP_TIMEOUT:-120}"
WORLD_INIT_TIMEOUT="${WORLD_INIT_TIMEOUT:-1800}"
REHEARSAL_BOTS="${REHEARSAL_BOTS:-50}"
MIN_FREE_GB="${MIN_FREE_GB:-30}"
# Rehearsal containers are capped at 5g (worldserver) + 1536m (drill DB); the
# gate covers both so a rehearsal cannot push the live host into swap/OOM.
MIN_REHEARSAL_MEM_GB="${MIN_REHEARSAL_MEM_GB:-7}"
IMAGES=(worldserver authserver db-import client-data)
BUILD_SERVICES=(ac-worldserver ac-authserver ac-db-import ac-client-data-init)
MODULE_CONFS=("mod-playerbots:playerbots" "mod-individual-progression:individualProgression" "mod-ah-bot-plus:mod_ahbot")
ETC_CONFS=(worldserver authserver dbimport)
REHEARSAL_NET="acup-rehearsal-net"
# The admin stack is optional: recorded at cutover start (image + compose file).
ADMIN_PRESENT=true
CLIENT_DATA_FACTS=(CLIENT_DATA_LIVE CLIENT_DATA_TARGET CLIENT_DATA_ROLLBACK_VOLUME CLIENT_DATA_TARGET_VOLUME)

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/upstream-versions.sh"

# load_versions — pinned revisions plus the candidate image tag derived from them.
load_versions() {
    load_upstream_versions "$VERSIONS_FILE" || return 1
    CANDIDATE_TAG="upgrade-${AC_CORE_REV:0:12}"
}

CMD="${1:-}"
[ $# -gt 0 ] && shift
DRY_RUN=false
YES=false
ALLOW_ONLINE=false
SKIP_REHEARSAL=false
NO_SAFETY_BACKUP=false
ACCEPT_VERIFY_FAILURE=false
PREVIOUS_REPO_REV=""
STATE_DIR=""
ARCHIVE=""
PINS=()

usage() {
    sed -n '/^# Usage:/,/^# Work root:/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --yes) YES=true; shift ;;
        --allow-online-players) ALLOW_ONLINE=true; shift ;;
        --skip-rehearsal) SKIP_REHEARSAL=true; shift ;;
        --no-safety-backup) NO_SAFETY_BACKUP=true; shift ;;
        --accept-verify-failure) ACCEPT_VERIFY_FAILURE=true; shift ;;
        --previous-repo-rev) PREVIOUS_REPO_REV="${2:?}"; shift 2 ;;
        --state) STATE_DIR="${2:?}"; shift 2 ;;
        --archive) ARCHIVE="${2:?}"; shift 2 ;;
        --pin) PINS+=("${2:?}"); shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1" >&2; usage ;;
    esac
done

PIN_ARGS=()
for pin in ${PINS[@]+"${PINS[@]}"}; do
    # Same rule as migrate-ac-env.sh; checked here because pins are recorded in state.env.
    if ! [[ "$pin" =~ ^AC_[A-Z0-9_]+=[A-Za-z0-9._,:-]*$ ]]; then
        echo "ERROR: invalid --pin '$pin' (expected AC_KEY=VALUE with a shell-safe value)" >&2
        exit 2
    fi
    PIN_ARGS+=(--pin "$pin")
done

# ---------------------------------------------------------------- helpers ---
log() { echo "[$(date -u '+%F %T')Z] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
CHECK_FAILS=0
check_ok() { echo "  [OK]   $*"; }
check_fail() { echo "  [FAIL] $*"; CHECK_FAILS=$((CHECK_FAILS + 1)); }
check_info() { echo "  [INFO] $*"; }
# check_that OK_MSG FAIL_MSG CMD... — record one check from a command's status.
check_that() {
    local pass="$1" failmsg="$2"
    shift 2
    if "$@"; then check_ok "$pass"; else check_fail "$failmsg"; fi
}

# run CMD... — every state-changing command goes through here.
run() {
    if [ "$DRY_RUN" = true ]; then
        printf '+ %s\n' "$(printf '%q ' "$@")"
    else
        "$@"
    fi
}

confirm() {
    local word="$1" reply
    [ "$YES" = true ] && return 0
    [ "$DRY_RUN" = true ] && return 0
    read -r -p "Type ${word} to continue: " reply
    [ "$reply" = "$word" ] || die "aborted (confirmation not given)"
}

compose() { (cd "$STACK_DIR" && docker compose "$@"); }
have() { command -v "$1" >/dev/null 2>&1; }

env_value() {
    { grep -E "^$1=" "$STACK_DIR/.env" 2>/dev/null || true; } | tail -n 1 | cut -d= -f2-
}

# db_query SQL — read-only query against the live database container.
db_query() {
    docker exec ac-database sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -B -e "$1" 2>/dev/null' sh "$1"
}

container_status() {
    docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo missing
}

# wait_world_initialized CONTAINER TIMEOUT — only trusts the current boot.
wait_world_initialized() {
    local name="$1" timeout="$2" started deadline status
    started="$(docker inspect -f '{{.State.StartedAt}}' "$name" 2>/dev/null)" || return 1
    deadline=$(( $(date +%s) + timeout ))
    # Look at least once before testing the deadline: a short timeout must not
    # expire across a second boundary before the first look.
    while :; do
        status="$(container_status "$name")"
        if [ "$status" != running ]; then
            echo "ERROR: $name entered '$status' before World Initialized." >&2
            return 1
        fi
        # Process substitution, not a pipe: with pipefail, grep -q closing the
        # pipe early makes `docker logs` die of SIGPIPE (141) on a long log.
        if grep -q "World Initialized" < <(docker logs --since "$started" "$name" 2>&1); then
            return 0
        fi
        [ "$(date +%s)" -lt "$deadline" ] || break
        sleep 10
    done
    echo "ERROR: $name did not log World Initialized within ${timeout}s." >&2
    return 1
}

compose_scale_args() {
    local args=() svc services
    services="$(compose config --services 2>/dev/null || echo "")"
    for svc in phpmyadmin ac-eluna-ts-dev; do
        if echo "$services" | grep -qx "$svc"; then
            args+=("--scale" "$svc=0")
        fi
    done
    if [ "${#args[@]}" -gt 0 ]; then
        printf '%s\n' "${args[@]}"
    fi
}

humans_online() {
    db_query "SELECT COUNT(*) FROM acore_characters.characters c JOIN acore_auth.account a ON a.id=c.account WHERE c.online=1 AND a.username NOT LIKE 'RNDBOT%';" | tail -n 1
}

client_data_volume() {
    printf '%s_ac-client-data\n' "$(env_value COMPOSE_PROJECT_NAME || true)" | sed 's/^_/azerothcore_/'
}

# volume_data_version VOLUME IMAGE — INSTALLED_VERSION recorded in a data volume.
volume_data_version() {
    docker run --rm --network none --user 0 -v "$1:/d:ro" --entrypoint sh "$2" \
        -c 'cat /d/data-version 2>/dev/null' | sed -n 's/^INSTALLED_VERSION=//p'
}

# volume_copy_facts VOLUME IMAGE — read-only facts about a client-data volume:
# its data-version lines, the rollback-copy completion marker (MARKER_FILES,
# MARKER_BYTES) and its own tree (TREE_FILES = entries, TREE_BYTES = bytes in
# regular files), both without the marker. Regular-file bytes, not `du -sb`:
# directory sizes legitimately differ between a tree and its `cp -a` copy.
volume_copy_facts() {
    docker run --rm --network none --user 0 -v "$1:/d:ro" --entrypoint sh "$2" -c '
        cat /d/data-version 2>/dev/null; echo
        if [ -f /d/.acup-complete ]; then sed "s/^/MARKER_/" /d/.acup-complete; fi
        echo "TREE_FILES=$(find /d -mindepth 1 ! -path /d/.acup-complete | wc -l)"
        echo "TREE_BYTES=$(find /d -type f ! -path /d/.acup-complete -printf "%s\n" | awk "{s+=\$1} END {printf \"%.0f\\n\", s}")"'
}

# COPY_WITH_MARKER_CMD — container command for `-v SRC:/src:ro -v DST:/dst`:
# empty DST, copy SRC, then record SRC's entry count and byte total (marker
# excluded) in DST/.acup-complete. Only a copy that finished carries the marker.
# The expressions expand in the container's shell, not here.
# shellcheck disable=SC2016
{
    COPY_WITH_MARKER_CMD='find /dst -mindepth 1 -delete && cp -a /src/. /dst/'
    COPY_WITH_MARKER_CMD+=' && files=$(find /src -mindepth 1 ! -path /src/.acup-complete | wc -l)'
    COPY_WITH_MARKER_CMD+=' && bytes=$(find /src -type f ! -path /src/.acup-complete -printf "%s\n" | awk "{s+=\$1} END {printf \"%.0f\\n\", s}")'
    COPY_WITH_MARKER_CMD+=' && printf "FILES=%s\nBYTES=%s\n" "$files" "$bytes" > /dst/.acup-complete'
}

# is_exact_integer VALUE — true for digits only. A byte total in a copy marker
# is compared as a string, so a rounded one (an older script's awk printed
# BYTES=3.23188e+09 through %.6g) must never count as a number to match.
is_exact_integer() {
    case "$1" in ''|*[!0-9]*) return 1 ;; esac
}

# rollback_copy_problem VOLUME VERSION IMAGE [LIVE_VOLUME] — print why VOLUME
# is not a complete rollback copy of the client data VERSION (and return 1):
# it must exist, hold VERSION, carry the completion marker and match the
# marker's numbers — and LIVE_VOLUME's, when given (before the live data is
# switched; afterwards only the marker can vouch for the copy). All read-only.
rollback_copy_problem() {
    local vol="$1" want="$2" image="$3" live="${4:-}" facts found mf mb tf tb lf lb
    if ! docker volume inspect "$vol" >/dev/null 2>&1; then
        echo "volume $vol is missing"; return 1
    fi
    facts="$(volume_copy_facts "$vol" "$image")" || { echo "cannot read volume $vol"; return 1; }
    found="$(sed -n 's/^INSTALLED_VERSION=//p' <<< "$facts" | head -n 1)"
    mf="$(sed -n 's/^MARKER_FILES=//p' <<< "$facts")"; mb="$(sed -n 's/^MARKER_BYTES=//p' <<< "$facts")"
    tf="$(sed -n 's/^TREE_FILES=//p' <<< "$facts")"; tb="$(sed -n 's/^TREE_BYTES=//p' <<< "$facts")"
    if [ -z "$want" ] || [ "$found" != "$want" ]; then
        echo "$vol holds '${found:-no data-version}', not the recorded pre-upgrade version '${want:-unknown}'"; return 1
    fi
    if [ -z "$mf" ] || [ -z "$mb" ]; then
        echo "$vol has no completion marker (.acup-complete): its copy never finished"; return 1
    fi
    if ! is_exact_integer "$mb"; then
        echo "$vol has a completion marker with an inexact byte total (BYTES=$mb, written by an older version of this script): its copy cannot be verified"; return 1
    fi
    if [ "$tf $tb" != "$mf $mb" ]; then
        echo "$vol holds ${tf:-?} entries / ${tb:-?} bytes but its completion marker says $mf / $mb"; return 1
    fi
    if [ -n "$live" ]; then
        facts="$(volume_copy_facts "$live" "$image")" || { echo "cannot read volume $live"; return 1; }
        lf="$(sed -n 's/^TREE_FILES=//p' <<< "$facts")"; lb="$(sed -n 's/^TREE_BYTES=//p' <<< "$facts")"
        if [ "$tf $tb" != "$lf $lb" ]; then
            echo "$vol holds $tf entries / $tb bytes but the live client data $live holds ${lf:-?} / ${lb:-?}"; return 1
        fi
    fi
}

# restored_client_data_problem LIVE COPY VERSION IMAGE — after COPY was copied
# back into LIVE: print why LIVE does not hold VERSION with exactly the entries
# and bytes COPY's completion marker records (and return 1). Read-only.
restored_client_data_problem() {
    local live="$1" copy="$2" want="$3" image="$4" facts found mf mb lf lb
    facts="$(volume_copy_facts "$copy" "$image")" || { echo "cannot read volume $copy"; return 1; }
    mf="$(sed -n 's/^MARKER_FILES=//p' <<< "$facts")"; mb="$(sed -n 's/^MARKER_BYTES=//p' <<< "$facts")"
    facts="$(volume_copy_facts "$live" "$image")" || { echo "cannot read volume $live"; return 1; }
    found="$(sed -n 's/^INSTALLED_VERSION=//p' <<< "$facts" | head -n 1)"
    lf="$(sed -n 's/^TREE_FILES=//p' <<< "$facts")"; lb="$(sed -n 's/^TREE_BYTES=//p' <<< "$facts")"
    if [ -z "$want" ] || [ "$found" != "$want" ]; then
        echo "$live holds '${found:-no data-version}', not the pre-upgrade version '${want:-unknown}'"; return 1
    fi
    if [ -z "$mf" ] || ! is_exact_integer "$mb" || [ "$lf $lb" != "$mf $mb" ]; then
        echo "$live holds ${lf:-?} entries / ${lb:-?} bytes but the completion marker of $copy says ${mf:-?} / ${mb:-?}"; return 1
    fi
}

image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }

state_set() {
    [ "$DRY_RUN" = true ] && return 0
    printf '%s=%s\n' "$1" "$2" >> "$STATE_DIR/state.env"
}
state_get() {
    [ -f "$STATE_DIR/state.env" ] || return 0
    { grep -E "^$1=" "$STATE_DIR/state.env" || true; } | tail -n 1 | cut -d= -f2-
}
# state_mark KEY — set a marker (KEY=1) and flush state.env to disk before the
# action it guards: after a power loss the markers must still say what changed.
state_mark() {
    state_set "$1" 1
    [ "$DRY_RUN" = true ] && return 0
    sync "$STATE_DIR/state.env" 2>/dev/null || sync
}
step_done() { [ "$(state_get "STEP_$1")" = 1 ]; }
mark_done() { state_mark "STEP_$1"; }
# Started markers (SWITCH_CODE_STARTED, CLIENT_DATA_SWITCH_STARTED,
# IMAGES_SWITCH_STARTED, BOOT_STARTED, ADMIN_STEP_STARTED) are written
# immediately BEFORE a step's first mutation, so rollback knows what may have
# changed even when the step died before its STEP_ marker.
mark_started() { state_mark "$1"; }
started() { [ "$(state_get "$1")" = 1 ]; }
# recorded_pins — the --pin values the cutover started with, one per line.
recorded_pins() {
    if [ -f "$STATE_DIR/state.env" ]; then
        { grep '^PIN=' "$STATE_DIR/state.env" || true; } | cut -d= -f2-
    fi
}

# cutover_fact KEY — a staging fact recorded in the cutover state (the staging
# file itself during a dry run, which records no state).
cutover_fact() {
    if [ "$DRY_RUN" = true ] && [ ! -f "$STATE_DIR/state.env" ]; then
        stage_get "$1"
    else
        state_get "$1"
    fi
}

resolve_state_dir() {
    if [ -z "$STATE_DIR" ]; then
        [ -L "$UPGRADE_ROOT/current" ] || die "no current upgrade state; pass --state DIR"
        STATE_DIR="$(readlink -f "$UPGRADE_ROOT/current")"
    fi
    [ -f "$STATE_DIR/state.env" ] || die "not an upgrade state directory: $STATE_DIR"
}

ensure_root() {
    run mkdir -p "$UPGRADE_ROOT"
    run chmod 700 "$UPGRADE_ROOT"
}

# take_lock — one state-changing run at a time: a rollback racing a resumed
# cutover would restore the databases under a booting worldserver.
take_lock() {
    [ "$DRY_RUN" = true ] && return 0
    ensure_root
    exec 9>>"$UPGRADE_ROOT/.lock"
    flock -n 9 || die "another upgrade-azerothcore.sh run holds $UPGRADE_ROOT/.lock"
}

# refuse_rolled_back — a rolled-back cutover is closed: resuming it would boot a
# half-switched stack, rolling it back again would restore the pre-upgrade dump
# over everything played since the first rollback.
refuse_rolled_back() {
    if [ "$(state_get ROLLED_BACK)" = 1 ]; then
        die "the upgrade in $STATE_DIR was already rolled back; start a new cutover (without --state) to upgrade again"
    fi
}

# stop_realm_and_die MESSAGE — a failed boot or verification leaves the realm
# stopped (never half-up on an unverified version), then dies with both ways on.
# A stop that did not take is reported, never claimed.
stop_realm_and_die() {
    local name still=""
    trap - ERR
    # The booted realm may be mid-saveall with bots online: give it the full
    # stop timeout (Docker's 10 s default would SIGKILL it), then the authserver.
    compose stop -t "$STOP_TIMEOUT" ac-worldserver || true
    compose stop ac-authserver || true
    for name in ac-worldserver ac-authserver; do
        case "$(container_status "$name")" in
            exited|created|missing) ;;
            *) still="${still:+$still, }$name" ;;
        esac
    done
    if [ -n "$still" ]; then
        die "$1 STILL RUNNING: ${still} (the stop failed; stop it by hand: cd $STACK_DIR && docker compose stop -t $STOP_TIMEOUT ac-worldserver ac-authserver). Then fix and re-run: $0 cutover --state $STATE_DIR — or roll back: $0 rollback --state $STATE_DIR"
    fi
    die "$1 Server is stopped. Fix and re-run: $0 cutover --state $STATE_DIR — or roll back: $0 rollback --state $STATE_DIR"
}

# boot_failure_trap STEP — ERR trap (with errtrace) for the boot and verify
# steps: any unexpected failure there (a count query, a step marker) stops the
# realm like a failed check does. Inside a subshell or command substitution it
# only ends that subshell; the failing command in the main shell trips it again.
boot_failure_trap() {
    local rc=$?
    [ "$BASH_SUBSHELL" -eq 0 ] || exit "$rc"
    stop_realm_and_die "unexpected failure (exit $rc) during the $1 step."
}

# utc_minute_of_day — minutes since 00:00 UTC. UPGRADE_NOW_UTC_MIN is a test
# seam only; it never needs to be set for real runs.
utc_minute_of_day() {
    if [ -n "${UPGRADE_NOW_UTC_MIN:-}" ]; then
        [[ "$UPGRADE_NOW_UTC_MIN" =~ ^[0-9]+$ ]] || die "UPGRADE_NOW_UTC_MIN must be a number of minutes"
        echo "$UPGRADE_NOW_UTC_MIN"
    else
        echo $(( 10#$(date -u +%H) * 60 + 10#$(date -u +%M) ))
    fi
}

# in_nightly_backup_window — 02:30-03:45 UTC: the 03:00 cron backup would race
# a cold backup, db-import DDL or a stopped database.
in_nightly_backup_window() {
    local now
    now="$(utc_minute_of_day)" || exit 1
    [ "$now" -ge 150 ] && [ "$now" -lt 225 ]
}

# rollback_exit_hint — rollback is idempotent until it records ROLLED_BACK.
rollback_exit_hint() {
    local rc=$?
    [ "$rc" -ne 0 ] || return 0
    [ "$(state_get ROLLED_BACK)" = 1 ] && return 0
    echo "" >&2
    if [ "$(state_get ROLLBACK_RESTORED)" = 1 ]; then
        {
            echo "The stack is restored to the pre-upgrade state and was started; only its verification is open (exit $rc)."
            echo "  Read $STATE_DIR/logs/rollback-verify.log, fix the cause, then verify again (nothing is restored twice):"
            echo "    $0 rollback --state $STATE_DIR"
            echo "  Or, if you judge the failed checks harmless, close the rollback:"
            echo "    $0 rollback --state $STATE_DIR --accept-verify-failure"
        } >&2
        return 0
    fi
    echo "Rollback did not finish (exit $rc); it is safe to re-run: $0 rollback --state $STATE_DIR" >&2
}

# refuse_rollback_in_progress — once a rollback has begun, only the rollback may
# continue (re-runs are allowed); resuming or finalizing would act on a
# half-restored stack.
refuse_rollback_in_progress() {
    if [ "$(state_get ROLLBACK_STARTED)" = 1 ]; then
        die "a rollback was started for this cutover; finish it: $0 rollback --state $STATE_DIR"
    fi
}

# unfinished_current_cutover — print why $UPGRADE_ROOT/current is a cutover that
# neither completed nor was rolled back, or whose rollback is unfinished (and
# return 1); prints nothing otherwise. Read-only.
unfinished_current_cutover() {
    local previous_state
    [ -L "$UPGRADE_ROOT/current" ] || return 0
    previous_state="$(readlink -f "$UPGRADE_ROOT/current")"
    [ -f "$previous_state/state.env" ] || return 0
    grep -qx 'ROLLED_BACK=1' "$previous_state/state.env" && return 0
    if grep -qx 'ROLLBACK_STARTED=1' "$previous_state/state.env"; then
        echo "a rollback was started for the cutover in $previous_state; finish it: $0 rollback --state $previous_state"
        return 1
    fi
    if ! grep -qx 'CUTOVER_OK=1' "$previous_state/state.env"; then
        echo "the cutover in $previous_state is incomplete; resume it ($0 cutover --state $previous_state) or roll it back ($0 rollback --state $previous_state)"
        return 1
    fi
}

# cutover_exit_hint — on any failed exit after the state exists, say how to go on.
cutover_exit_hint() {
    local rc=$?
    [ "$rc" -ne 0 ] || return 0
    [ -n "$STATE_DIR" ] && [ -f "$STATE_DIR/state.env" ] || return 0
    {
        echo ""
        echo "Cutover stopped (exit $rc); the server may be down. State: $STATE_DIR"
        echo "  Fix the cause, then resume:  $0 cutover --state $STATE_DIR"
        echo "  Or return to the old version: $0 rollback --state $STATE_DIR"
    } >&2
}

# module_conf_matches_dist_except_guids CONF DIST — mod_ahbot.conf may only
# differ from its template in the installer-written AuctionHouseBot.GUIDs.
module_conf_matches_dist_except_guids() {
    diff -q <(grep -v 'AuctionHouseBot.GUIDs' "$1" | grep -v '^[[:space:]]*$') \
            <(grep -v 'AuctionHouseBot.GUIDs' "$2" | grep -v '^[[:space:]]*$') >/dev/null
}

# module_diff_is_only_managed_patches MODULE_DIR — tracked changes are allowed
# only when they are exactly this repository's applied local patches.
module_diff_is_only_managed_patches() {
    local dir="$1" module patch patched_files changed
    git -C "$dir" diff --quiet && return 0
    module="$(basename "$dir")"
    patched_files=""
    for patch in "$SCRIPT_DIR/patches/$module"/*.patch; do
        [ -f "$patch" ] || continue
        git -C "$dir" apply --reverse --check "$patch" 2>/dev/null || return 1
        patched_files+="$(git -C "$dir" apply --numstat "$patch" | awk '{print $3}')"$'\n'
    done
    changed="$(git -C "$dir" diff --name-only)"
    [ -z "$(comm -23 <(printf '%s\n' "$changed" | sort -u) <(printf '%s' "$patched_files" | sort -u))" ]
}

# live_switch_collisions DIR REV — print "untracked: PATH" / "ignored: PATH" for
# every local file in DIR's working tree that a checkout of REV would have to
# create or overwrite. Paths are compared as files, not directories: a nested
# repository (git lists it as "dir/", e.g. modules/mod-* under the core) or any
# other directory collides only when REV tracks a path inside it, a file only
# when REV tracks that path or a path below it, or a file where REV needs a
# directory. Returns 2 when git cannot list the trees (never "no collisions").
live_switch_collisions() {
    local dir="$1" rev="$2" tracked untracked ignored
    tracked="$(git -C "$dir" ls-tree -r --name-only "$rev")" || return 2
    untracked="$(git -C "$dir" ls-files --others --exclude-standard)" || return 2
    ignored="$(git -C "$dir" ls-files --others --ignored --exclude-standard)" || return 2
    awk '
        $0 == "" { next }
        FILENAME == ARGV[1] {
            tracked[$0] = 1
            p = $0
            while (sub(/\/[^\/]*$/, "", p)) tracked_dir[p] = 1
            next
        }
        {
            path = $0
            sub(/\/$/, "", path)
            hit = (path in tracked) || (path in tracked_dir)
            p = path
            while (!hit && sub(/\/[^\/]*$/, "", p)) hit = (p in tracked)
            if (hit) print ((FILENAME == ARGV[2]) ? "untracked: " : "ignored: ") $0
        }
    ' <(printf '%s\n' "$tracked") <(printf '%s\n' "$untracked") <(printf '%s\n' "$ignored")
}

# check_live_switch_collisions — prove, before any downtime, that switching the
# live checkouts to the pinned revisions cannot collide with local files: an
# untracked one makes `checkout -B` fail mid-cutover, an ignored one would be
# silently overwritten. Needs the pinned commits in the live checkouts (stage
# fetches them); a dry-run stage has not fetched them yet and only notes that.
check_live_switch_collisions() {
    local label rel url branch rev dir hits n fails=0
    echo "== Live checkout switch check"
    while IFS='|' read -r label rel url branch rev <&3; do
        dir="$STACK_DIR/$rel"
        if ! git -C "$dir" cat-file -e "${rev}^{commit}" 2>/dev/null; then
            if [ "$DRY_RUN" = true ]; then
                check_info "$label: pinned ${rev:0:12} not fetched into $dir yet (dry-run); not checked"
            else
                check_fail "$label: pinned ${rev:0:12} is not present in $dir; run 'stage' first"
                fails=$((fails + 1))
            fi
            continue
        fi
        if ! hits="$(live_switch_collisions "$dir" "$rev")"; then
            check_fail "$label: cannot compare $dir with ${rev:0:12}"
            fails=$((fails + 1))
        elif [ -z "$hits" ]; then
            check_ok "$label: no local file collides with ${rev:0:12}"
        else
            n="$(printf '%s\n' "$hits" | wc -l)"
            check_fail "$label: ${n} local file(s) in $dir collide with paths tracked at ${rev:0:12} (untracked: the checkout would fail mid-cutover; ignored: it would be overwritten):"
            sed -n '1,20s/^/           /p' <<< "$hits"
            if [ "$n" -gt 20 ]; then echo "           ... and $((n - 20)) more"; fi
            fails=$((fails + 1))
        fi
    done 3< <(upstream_repo_specs)
    if [ "$fails" -gt 0 ]; then
        echo "  Move or remove the files listed above (or commit them upstream), then re-run."
        return 1
    fi
}

# -------------------------------------------------------------- preflight ---
# previous_repo_rev_mismatches — when the --previous-repo-rev tooling pins
# upstream revisions, print every repository whose pin is not the live HEAD.
previous_repo_rev_mismatches() {
    local versions label rel url branch rev key pinned live
    versions="$(git -C "$REPO_DIR" show "${PREVIOUS_REPO_REV}:scripts/upstream-versions.env" 2>/dev/null)" || return 0
    while IFS='|' read -r label rel url branch rev <&3; do
        case "$label" in
            core) key=AC_CORE_REV ;;
            mod-playerbots) key=MOD_PLAYERBOTS_REV ;;
            mod-ah-bot-plus) key=MOD_AHBOT_REV ;;
            *) key=MOD_IP_REV ;;
        esac
        pinned="$(sed -n "s/^${key}=//p" <<< "$versions" | tail -n 1)"
        live="$(git -C "$STACK_DIR/$rel" rev-parse HEAD 2>/dev/null || echo unknown)"
        if [ "$pinned" != "$live" ]; then
            echo "$label: pinned ${pinned:0:12} at ${PREVIOUS_REPO_REV:0:12}, live ${live:0:12}"
        fi
    done 3< <(upstream_repo_specs)
}

preflight_checks() {
    local mode="$1" label rel url branch rev dir free_gb name humans mismatches
    CHECK_FAILS=0
    echo "== Preflight (${mode})"
    for tool in docker git python3 flock sha256sum cmp comm; do
        check_that "tool: $tool" "missing tool: $tool" have "$tool"
    done
    if load_versions; then
        check_ok "pinned versions: core ${AC_CORE_REV:0:12}, client data ${CLIENT_DATA_VERSION}"
    else
        check_fail "pinned versions file invalid"
        return 1
    fi
    if [ -n "$PREVIOUS_REPO_REV" ] && git -C "$REPO_DIR" cat-file -e "${PREVIOUS_REPO_REV}^{commit}" 2>/dev/null; then
        check_ok "previous repository revision ${PREVIOUS_REPO_REV:0:12} found (rollback tooling snapshot)"
        # Its verifier judges the rollback: tooling pinned to other revisions
        # would fail a correctly rolled-back stack on every attempt.
        mismatches="$(previous_repo_rev_mismatches)"
        if [ -n "$mismatches" ]; then
            check_fail "previous-repo-rev is not the tooling the live stack was installed with:"
            sed -n 's/^/           /p' <<< "$mismatches"
        elif git -C "$REPO_DIR" cat-file -e "${PREVIOUS_REPO_REV}:scripts/upstream-versions.env" 2>/dev/null; then
            check_ok "previous repository revision pins the live checkouts' revisions"
        else
            check_info "previous repository revision pins no upstream revisions (from before pinning); not compared"
        fi
    else
        check_fail "--previous-repo-rev must name the wow-server-sp commit the live stack was installed with"
    fi
    for name in ac-database ac-worldserver ac-authserver; do
        check_that "$name running" "$name is not running" [ "$(container_status "$name")" = running ]
    done
    if [ "$(container_status azerothcore-admin)" = running ]; then
        check_ok "azerothcore-admin running"
    else
        check_info "azerothcore-admin is not running"
    fi
    if wait_world_initialized ac-worldserver 1 >/dev/null 2>&1; then
        check_ok "ac-worldserver reached World Initialized this boot"
    else
        check_fail "ac-worldserver has not logged World Initialized this boot"
    fi
    if [ -f "$STACK_DIR/logs/Errors.log" ]; then
        check_info "Errors.log is $(stat -c%s "$STACK_DIR/logs/Errors.log") bytes"
    fi
    humans="$(humans_online || echo unknown)"
    if [ "$humans" = 0 ]; then
        check_ok "no real players online"
    elif [ "$mode" = cutover ] && [ "$ALLOW_ONLINE" != true ]; then
        check_fail "${humans} real-player character(s) online; announce and wait, or pass --allow-online-players"
    else
        check_info "${humans} real-player character(s) online"
    fi
    for dir in "$(dirname "$UPGRADE_ROOT")" /var/lib/docker; do
        free_gb="$({ df -Pk "$dir" 2>/dev/null || true; } | awk 'NR==2 {print int($4/1048576)}')"
        if [ -n "$free_gb" ] && [ "$free_gb" -ge "$MIN_FREE_GB" ]; then
            check_ok "${free_gb} GB free on the filesystem of $dir"
        elif [ -z "$free_gb" ]; then
            check_info "cannot read free space for $dir"
        else
            check_fail "only ${free_gb} GB free on the filesystem of $dir (need ${MIN_FREE_GB})"
        fi
    done
    for name in "${ETC_CONFS[@]}"; do
        if [ ! -f "$STACK_DIR/env/dist/etc/$name.conf" ] || cmp -s "$STACK_DIR/env/dist/etc/$name.conf" "$STACK_DIR/env/dist/etc/$name.conf.dist"; then
            check_ok "env/dist/etc/$name.conf has no local edits"
        else
            check_fail "env/dist/etc/$name.conf differs from its .dist; merge local edits into AC_* overrides first"
        fi
    done
    for spec in "${MODULE_CONFS[@]}"; do
        name="${spec#*:}"
        if [ "$name" = mod_ahbot ]; then
            check_that "configs/modules/$name.conf differs from its .dist only in AuctionHouseBot.GUIDs" \
                "configs/modules/$name.conf has edits beyond AuctionHouseBot.GUIDs" \
                module_conf_matches_dist_except_guids "$STACK_DIR/configs/modules/$name.conf" "$STACK_DIR/configs/modules/$name.conf.dist"
        else
            check_that "configs/modules/$name.conf has no local edits" \
                "configs/modules/$name.conf differs from its .dist; move the edits to AC_* overrides first" \
                cmp -s "$STACK_DIR/configs/modules/$name.conf" "$STACK_DIR/configs/modules/$name.conf.dist"
        fi
    done
    while IFS='|' read -r label rel url branch rev <&3; do
        dir="$STACK_DIR/$rel"
        if [ "$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null)" != "$branch" ]; then
            check_fail "$label: $dir is not on branch $branch"
        elif ! git -C "$dir" remote get-url origin 2>/dev/null | grep -qF "${url%.git}"; then
            check_fail "$label: $dir origin is not $url"
        elif [ "$label" = core ] && ! git -C "$dir" diff --quiet; then
            check_fail "core: tracked files in $dir have local modifications"
        elif [ "$label" != core ] && ! module_diff_is_only_managed_patches "$dir"; then
            check_fail "$label: $dir has local modifications that are not managed patches"
        else
            check_ok "$label: $dir clean at $(git -C "$dir" rev-parse --short=12 HEAD)"
        fi
    done 3< <(upstream_repo_specs)
    if cmp -s "$STACK_DIR/backup.sh" "$SCRIPT_DIR/backup.sh"; then
        check_ok "deployed backup.sh matches the repository"
    else
        check_fail "deployed $STACK_DIR/backup.sh differs from scripts/backup.sh; deploy it first"
    fi
    if crontab -l 2>/dev/null | grep -v '^[[:space:]]*#' | grep -qF "$STACK_DIR/backup.sh"; then
        check_ok "nightly backup cron entry present"
    else
        check_fail "nightly backup cron entry missing"
    fi
    if [ "$mode" = cutover ]; then
        if in_nightly_backup_window; then
            check_fail "02:30-03:45 UTC overlaps the 03:00 nightly backup; pick another time"
        else
            check_ok "outside the nightly backup window"
        fi
    fi
    echo "== Preflight: $([ "$CHECK_FAILS" -eq 0 ] && echo PASS || echo "FAIL (${CHECK_FAILS})")"
    [ "$CHECK_FAILS" -eq 0 ]
}

cmd_preflight() { preflight_checks preflight; }

# ------------------------------------------------------------------ stage ---
staging_src() { printf '%s/staging/src-%s\n' "$UPGRADE_ROOT" "${AC_CORE_REV:0:12}"; }
stage_env() { printf '%s/staging/stage-%s.env\n' "$UPGRADE_ROOT" "${AC_CORE_REV:0:12}"; }

stage_get() { file_get "$(stage_env)" "$1"; }

# file_get FILE KEY — the last KEY=VALUE in FILE (empty when absent).
file_get() {
    { grep -E "^$2=" "$1" 2>/dev/null || true; } | tail -n 1 | cut -d= -f2-
}

# build_inputs — what a stage builds the candidate from, one KEY=VALUE per
# line: the four pinned revisions, the client-data pin, the versions file,
# every local patch and every SQL patch (the pre-db-import SQL the rehearsal
# and the cutover run before db-import). The module revisions and patches are
# not in the candidate tag (core SHA only), so this, not the tag, identifies
# the build — and a rehearsal is only valid for the SQL it ran.
build_inputs() {
    printf 'AC_CORE_REV=%s\nMOD_PLAYERBOTS_REV=%s\nMOD_AHBOT_REV=%s\nMOD_IP_REV=%s\nCLIENT_DATA_VERSION=%s\n' \
        "$AC_CORE_REV" "$MOD_PLAYERBOTS_REV" "$MOD_AHBOT_REV" "$MOD_IP_REV" "$CLIENT_DATA_VERSION"
    printf 'VERSIONS_SHA256=%s\n' "$(sha256sum < "$VERSIONS_FILE" | cut -d' ' -f1)"
    printf 'PATCHES_SHA256=%s\n' "$(patch_listing | sha256sum | cut -d' ' -f1)"
    printf 'SQL_PATCHES_SHA256=%s\n' "$(sql_patch_listing | sha256sum | cut -d' ' -f1)"
}

# patch_listing — "PATCH <path> <sha256>" for every scripts/patches/**/*.patch, sorted.
patch_listing() {
    local patch
    [ -d "$SCRIPT_DIR/patches" ] || return 0
    (cd "$SCRIPT_DIR/patches" && find . -type f -name '*.patch' | LC_ALL=C sort | while IFS= read -r patch; do
        printf 'PATCH %s %s\n' "${patch#./}" "$(sha256sum < "$patch" | cut -d' ' -f1)"
    done)
}

# sql_patch_listing — "SQL <path> <sha256>" for every scripts/patches/sql/**/*.sql, sorted.
sql_patch_listing() {
    local file
    [ -d "$SCRIPT_DIR/patches/sql" ] || return 0
    (cd "$SCRIPT_DIR/patches/sql" && find . -type f -name '*.sql' | LC_ALL=C sort | while IFS= read -r file; do
        printf 'SQL %s %s\n' "${file#./}" "$(sha256sum < "$file" | cut -d' ' -f1)"
    done)
}

build_fingerprint() { build_inputs | sha256sum | cut -d' ' -f1; }

cmd_stage() {
    local src label rel url branch rev uid gid svc vol live_ver rollback_vol target_vol live_image problem
    load_versions
    # Mid-cutover or mid-rollback the live client data may be partial: a stage
    # copy would replace the only complete pre-upgrade copy with it.
    problem="$(unfinished_current_cutover)" || die "${problem}; finish or roll back that cutover first (nothing was staged)"
    src="$(staging_src)"
    ensure_root
    run mkdir -p "$UPGRADE_ROOT/staging"

    log "Staging checkout at pinned revisions: $src"
    while IFS='|' read -r label rel url branch rev <&3; do
        if [ "$DRY_RUN" = true ]; then
            run checkout_pinned_rev "$src/$rel" "$url" "$branch" "$rev"
        else
            checkout_pinned_rev "$src/$rel" "$url" "$branch" "$rev"
        fi
        log "  $label at ${rev:0:12}"
        # Fetch the pinned commit into the live checkout too (objects only; the
        # working tree is untouched), so cutover needs no network.
        if ! git -C "$STACK_DIR/$rel" cat-file -e "${rev}^{commit}" 2>/dev/null; then
            run git -C "$STACK_DIR/$rel" fetch -q --depth 1 origin "$rev"
        fi
    done 3< <(upstream_repo_specs)
    check_live_switch_collisions || die "local files collide with the pinned revisions; nothing was built"
    run "$SCRIPT_DIR/apply-local-patches.sh" "$src"

    for spec in "${MODULE_CONFS[@]}"; do
        if [ "${spec#*:}" = mod_ahbot ] && [ "$DRY_RUN" != true ]; then
            module_conf_matches_dist_except_guids "$STACK_DIR/configs/modules/mod_ahbot.conf" \
                "$src/modules/mod-ah-bot-plus/conf/mod_ahbot.conf.dist" \
                || die "the new mod_ahbot.conf.dist changed beyond GUIDs; merge configs/modules/mod_ahbot.conf by hand"
        fi
    done

    uid="$(env_value DOCKER_USER_ID)"; gid="$(env_value DOCKER_GROUP_ID)"
    log "Building candidate images (tag ${CANDIDATE_TAG}); the live server keeps running."
    if [ "$DRY_RUN" = true ]; then
        run docker compose -p azerothcore-upgrade-build -f "$src/docker-compose.yml" build "${BUILD_SERVICES[@]}"
    else
        (cd "$src" && DOCKER_IMAGE_TAG="$CANDIDATE_TAG" DOCKER_USER_ID="${uid:-1000}" DOCKER_GROUP_ID="${gid:-1000}" \
            docker compose -p azerothcore-upgrade-build -f docker-compose.yml build "${BUILD_SERVICES[@]}") \
            2>&1 | tee "$UPGRADE_ROOT/staging/build-${AC_CORE_REV:0:12}.log"
    fi

    vol="$(client_data_volume)"
    live_image="acore/ac-wotlk-client-data:$(env_value DOCKER_IMAGE_TAG)"
    # A failed read must reach the explanation below, not end the run silently.
    live_ver="$(volume_data_version "$vol" "$live_image")" || true
    rollback_vol="acup-client-data-rollback-${live_ver:-unknown}"
    target_vol="acup-client-data-${CLIENT_DATA_VERSION}"
    if [ "$live_ver" != "$CLIENT_DATA_VERSION" ]; then
        # The rollback copy is named and verified by this version.
        if [ -z "$live_ver" ] || [ "$live_ver" = unknown ]; then
            die "cannot read the live client data version; refusing to stage a client-data switch"
        fi
        log "Client data changes ${live_ver} -> ${CLIENT_DATA_VERSION}."
        # Only a copy that finished carries the completion marker: an existing
        # volume that is not a complete copy by its own marker (an interrupted
        # stage) is emptied and copied again. A complete copy is never
        # overwritten, not even when the live data differs from it: the live
        # volume may be the partial one (an interrupted switch or rollback copy).
        if problem="$(rollback_copy_problem "$rollback_vol" "$live_ver" "$live_image")"; then
            problem="$(rollback_copy_problem "$rollback_vol" "$live_ver" "$live_image" "$vol")" \
                || die "rollback copy and live client data differ: ${problem}. Inspect both; if the live client data is intact, remove $rollback_vol by hand and re-run stage to copy it again. Nothing was copied"
            log "  rollback volume $rollback_vol already holds a complete copy of the live client data"
        else
            log "  copying live client data to rollback volume $rollback_vol (read-only source; ${problem})"
            if ! docker volume inspect "$rollback_vol" >/dev/null 2>&1; then
                run docker volume create "$rollback_vol" >/dev/null
            fi
            run docker run --rm --network none --user 0 -v "$vol:/src:ro" -v "$rollback_vol:/dst" \
                --entrypoint sh "$live_image" -c "$COPY_WITH_MARKER_CMD"
            if [ "$DRY_RUN" != true ]; then
                problem="$(rollback_copy_problem "$rollback_vol" "$live_ver" "$live_image" "$vol")" \
                    || die "the rollback copy of the client data is not complete: ${problem}"
            fi
        fi
        if [ "$DRY_RUN" = true ] || [ "$(volume_data_version "$target_vol" "acore/ac-wotlk-client-data:${CANDIDATE_TAG}" || true)" != "$CLIENT_DATA_VERSION" ]; then
            log "  downloading ${CLIENT_DATA_VERSION} into $target_vol (about 1.2 GB)"
            run docker volume create "$target_vol" >/dev/null
            run docker run --rm --name acup-client-data-download -v "$target_vol:/azerothcore/env/dist/data" \
                "acore/ac-wotlk-client-data:${CANDIDATE_TAG}"
        fi
        if [ "$DRY_RUN" != true ]; then
            [ "$(volume_data_version "$target_vol" "acore/ac-wotlk-client-data:${CANDIDATE_TAG}")" = "$CLIENT_DATA_VERSION" ] \
                || die "$target_vol does not hold ${CLIENT_DATA_VERSION} after download"
        fi
    fi

    [ "$DRY_RUN" = true ] && return 0
    {
        echo "CANDIDATE_TAG=${CANDIDATE_TAG}"
        echo "STAGING_SRC=${src}"
        echo "STAGED_AT=$(date -u +%FT%TZ)"
        echo "CLIENT_DATA_LIVE=${live_ver}"
        echo "CLIENT_DATA_TARGET=${CLIENT_DATA_VERSION}"
        echo "CLIENT_DATA_ROLLBACK_VOLUME=$([ "$live_ver" != "$CLIENT_DATA_VERSION" ] && echo "$rollback_vol")"
        echo "CLIENT_DATA_TARGET_VOLUME=$([ "$live_ver" != "$CLIENT_DATA_VERSION" ] && echo "$target_vol")"
        for svc in "${IMAGES[@]}"; do
            echo "CANDIDATE_ID_${svc//-/_}=$(image_id "acore/ac-wotlk-${svc}:${CANDIDATE_TAG}")"
        done
        build_inputs
        echo "BUILD_FINGERPRINT=$(build_fingerprint)"
    } > "$(stage_env)"
    log "Stage complete: $(stage_env)"
}

require_stage() {
    local svc expected actual
    [ -f "$(stage_env)" ] || die "run 'stage' first (no $(stage_env))"
    for svc in "${IMAGES[@]}"; do
        expected="$(stage_get "CANDIDATE_ID_${svc//-/_}")"
        actual="$(image_id "acore/ac-wotlk-${svc}:${CANDIDATE_TAG}")"
        if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
            die "candidate image $svc changed or missing since stage; re-run stage"
        fi
    done
}

# require_stage_matches_pins — the stage was built from exactly the pinned
# versions, local patches and SQL patches loaded now (same core SHA, hence same
# tag, is not enough: a module pin or patch may have changed since).
require_stage_matches_pins() {
    local key value changed=""
    [ "$(stage_get BUILD_FINGERPRINT)" = "$(build_fingerprint)" ] && return 0
    while IFS='=' read -r key value; do
        [ "$(stage_get "$key")" = "$value" ] || changed="${changed:+$changed, }$key"
    done < <(build_inputs)
    die "the stage was built from other pinned versions or local/SQL patches than the ones loaded now (changed: ${changed:-build fingerprint}); re-run stage"
}

# rehearsal_problem — why rehearsal-latest is not a passing rehearsal of the
# staged build with this cutover's --pin set; prints nothing when it is.
rehearsal_problem() {
    local result="$UPGRADE_ROOT/rehearsal-latest/result.env" svc rehearsed wanted
    if [ ! -f "$result" ] || [ "$(file_get "$result" REHEARSAL_OK)" != 1 ]; then
        echo "none recorded"; return 1
    fi
    if [ "$(file_get "$result" CANDIDATE_TAG)" != "$CANDIDATE_TAG" ]; then
        echo "the last one rehearsed $(file_get "$result" CANDIDATE_TAG)"; return 1
    fi
    if [ -z "$(stage_get BUILD_FINGERPRINT)" ] || [ "$(file_get "$result" BUILD_FINGERPRINT)" != "$(stage_get BUILD_FINGERPRINT)" ]; then
        echo "the last one rehearsed another build (fingerprint differs from the stage)"; return 1
    fi
    for svc in "${IMAGES[@]}"; do
        if [ "$(file_get "$result" "CANDIDATE_ID_${svc//-/_}")" != "$(stage_get "CANDIDATE_ID_${svc//-/_}")" ]; then
            echo "the last one rehearsed another $svc image"; return 1
        fi
    done
    rehearsed="$({ grep '^PIN=' "$result" || true; } | cut -d= -f2- | LC_ALL=C sort)"
    wanted="$(printf '%s\n' ${PINS[@]+"${PINS[@]}"} | { grep . || true; } | LC_ALL=C sort)"
    if [ "$rehearsed" != "$wanted" ]; then
        echo "the last one ran with --pin [${rehearsed//$'\n'/ }], this cutover with [${wanted//$'\n'/ }]"; return 1
    fi
}

# ---------------------------------------------------------- pre-db-import ---
# scripts/patches/sql/pre-db-import/*.sql: data fixes the candidate db-import
# needs before it runs (0001 frees gameobject guids a core update hard-codes).
# `rehearse` runs them on the restored copy, `cutover` on ac-database in the
# boot step; both right before db-import. Each file must be idempotent (a
# resumed boot step applies it again, also after db-import ran) and carry
# exactly one `-- guard: <SELECT returning a count>` line: run after the file,
# that query must print 0. Their sha256s are in the build fingerprint.
PRE_DB_IMPORT_SQL_DIR="${SCRIPT_DIR}/patches/sql/pre-db-import"

# pre_db_import_guard FILE — print FILE's one guard query (return 1 otherwise).
pre_db_import_guard() {
    local guards
    guards="$(sed -n 's/^-- guard: //p' "$1")"
    if [ -z "$guards" ] || [ "$(wc -l <<< "$guards")" -ne 1 ]; then
        echo "ERROR: $(basename "$1") must carry exactly one '-- guard: <SELECT returning a count>' line" >&2
        return 1
    fi
    printf '%s\n' "$guards"
}

# apply_pre_db_import_sql LABEL MYSQL_CMD... — run every pre-db-import file,
# in file-name order, through MYSQL_CMD (a mysql client that reads SQL on stdin
# and takes `-e QUERY`), each followed by its guard query. Fails closed (returns
# 1): a file without exactly one guard line (every file is checked before the
# first one runs), a mysql error, a failing guard query, a count other than 0.
# A dry run prints the commands instead.
apply_pre_db_import_sql() {
    local label="$1" file guard count i files=() guards=()
    shift
    for file in "$PRE_DB_IMPORT_SQL_DIR"/*.sql; do
        [ -f "$file" ] || continue
        guard="$(pre_db_import_guard "$file")" || return 1
        files+=("$file"); guards+=("$guard")
    done
    for i in "${!files[@]}"; do
        file="${files[$i]}"; guard="${guards[$i]}"
        if [ "$DRY_RUN" = true ]; then
            printf '+ %s< %q\n' "$(printf '%q ' "$@")" "$file"
            printf '+ %s-e %q   # must print 0\n' "$(printf '%q ' "$@")" "$guard"
            continue
        fi
        log "  $(basename "$file") -> $label"
        if ! "$@" < "$file"; then
            echo "ERROR: $(basename "$file") failed against $label (mysql error above)" >&2
            return 1
        fi
        if ! count="$("$@" -e "$guard" < /dev/null)"; then
            echo "ERROR: the guard query of $(basename "$file") failed against $label" >&2
            return 1
        fi
        if [ "$count" != 0 ]; then
            echo "ERROR: the guard of $(basename "$file") counts '${count}' (must be 0) on $label: $guard" >&2
            return 1
        fi
    done
    log "  pre-db-import SQL: ${#files[@]} file(s) applied to $label, every guard 0"
}

# --------------------------------------------------------------- rehearse ---
REH_WORK=""
rehearsal_cleanup() {
    docker rm -f acup-rehearsal-world acup-rehearsal-dbimport >/dev/null 2>&1 || true
    docker rm -f -v acup-rehearsal-db >/dev/null 2>&1 || true
    docker network rm "$REHEARSAL_NET" >/dev/null 2>&1 || true
}

assert_rehearsal_isolated() {
    local name="$1" info
    info="$(docker inspect -f '{{.HostConfig.NetworkMode}}|{{json .HostConfig.PortBindings}}|{{range .Mounts}}{{.Type}}:{{.Name}}:{{.Source}}:{{.RW}};{{end}}' "$name")"
    case "$info" in
        "${REHEARSAL_NET}|{}|"*|"${REHEARSAL_NET}|null|"*) ;;
        *) rehearsal_cleanup; die "$name is not isolated: $info" ;;
    esac
    case "$info" in
        *docker.sock*|*/opt/stacks*|*:ac-*|*:azerothcore_ac-database*)
            rehearsal_cleanup; die "$name mounts live state: $info" ;;
    esac
}

# rehearsal_env OUT DB_PASSWORD — AC_* environment for the rehearsal
# worldserver: the live overlays after migration, pointed at the rehearsal DB.
rehearsal_env() {
    local out="$1" password="$2" src
    src="$(staging_src)"
    cp "$STACK_DIR/docker-compose.override.yml" "$REH_WORK/override.yml"
    cp "$STACK_DIR/docker-compose.admin.yml" "$REH_WORK/admin.yml" 2>/dev/null || printf 'services: {}\n' > "$REH_WORK/admin.yml"
    "$SCRIPT_DIR/migrate-ac-env.sh" --override "$REH_WORK/override.yml" --admin "$REH_WORK/admin.yml" \
        --backup-dir "$REH_WORK/pre-migrate" ${PIN_ARGS[@]+"${PIN_ARGS[@]}"} \
        --validate-against "$src/src/server/apps/worldserver/worldserver.conf.dist" \
        --validate-against "$src/modules/mod-playerbots/conf/playerbots.conf.dist" \
        --validate-against "$src/modules/mod-individual-progression/conf/individualProgression.conf.dist" \
        --validate-against "$src/modules/mod-ah-bot-plus/conf/mod_ahbot.conf.dist" >/dev/null
    # The render holds the live database password: owner-only, and the reader
    # below deletes it.
    (umask 077 && cd "$src" && docker compose --env-file "$STACK_DIR/.env" -f docker-compose.yml \
        -f "$REH_WORK/override.yml" -f "$REH_WORK/admin.yml" config --format json > "$REH_WORK/compose.json")
    python3 -I - "$REH_WORK/compose.json" "$src/conf/dist/env.ac" "$password" "$REHEARSAL_BOTS" \
        "$REH_WORK/override.yml" "$REH_WORK/admin.yml" "$REH_WORK/overlay-keys.txt" > "$out" <<'PY'
import json
import os
import re
import sys

compose, env_ac, password, bots, override, admin, overlay_keys = sys.argv[1:8]
with open(compose, encoding="utf-8") as handle:
    rendered = json.load(handle)
os.unlink(compose)  # it holds the live database password
env = {}
for line in open(env_ac, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        env[key] = value
service = rendered["services"]["ac-worldserver"]
raw = service.get("environment") or {}
if isinstance(raw, list):
    raw = dict(item.split("=", 1) for item in raw)
env.update({k: "" if v is None else str(v) for k, v in raw.items()})
# The overrides this server sets: AC_* names the (migrated) overlays define
# that reach the worldserver. Compose also folds the image's env.ac
# (AC_CCACHE, AC_RESTARTER_*: build/restarter settings, no config key) and the
# base file's paths into the render; those are not overrides.
named = set()
entry = re.compile(r"^\s*(?:-\s*)?['\"]?(AC_[A-Z0-9_]+)['\"]?\s*[:=]")
for path in (override, admin):
    for line in open(path, encoding="utf-8"):
        m = entry.match(line)
        if m:
            named.add(m.group(1))
with open(overlay_keys, "w", encoding="utf-8") as handle:
    handle.writelines(f"{key}\n" for key in sorted(named & set(raw)))
for key, value in list(env.items()):
    if key.endswith("_DATABASE_INFO"):
        parts = value.split(";")
        if len(parts) == 5:
            env[key] = ";".join(["acup-rehearsal-db", "3306", "root", password, parts[4]])
env["AC_PLAYERBOTS_MIN_RANDOM_BOTS"] = bots
env["AC_PLAYERBOTS_MAX_RANDOM_BOTS"] = bots
for key in sorted(env):
    if "\n" in env[key]:
        raise SystemExit(f"multi-line value for {key}")
    print(f"{key}={env[key]}")
PY
    chmod 600 "$out"
}

cmd_rehearse() {
    local src mem_gb archive started rc db_pw chars_before chars_after code
    load_versions
    require_stage
    require_stage_matches_pins
    src="$(staging_src)"
    archive="${ARCHIVE:-$(find "$STACK_DIR/backups" -maxdepth 1 -name 'azerothcore-backup-*.tar.gz' -printf '%T@ %p\n' | sort -nr | head -n 1 | cut -d' ' -f2-)}"
    [ -f "$archive" ] || die "no backup archive to rehearse with"
    mem_gb="$(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo)"
    [ "$mem_gb" -ge "$MIN_REHEARSAL_MEM_GB" ] || die "only ${mem_gb} GB RAM available; rehearsal needs ${MIN_REHEARSAL_MEM_GB}"
    if docker network inspect "$REHEARSAL_NET" >/dev/null 2>&1 || docker container inspect acup-rehearsal-db >/dev/null 2>&1; then
        die "rehearsal leftovers exist; remove them: docker rm -f -v acup-rehearsal-db acup-rehearsal-world acup-rehearsal-dbimport; docker network rm $REHEARSAL_NET"
    fi
    if [ "$DRY_RUN" = true ]; then
        log "dry-run: would rehearse ${CANDIDATE_TAG} against $archive"
        return 0
    fi
    REH_WORK="$UPGRADE_ROOT/rehearsal-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$REH_WORK"/{etc,logs,modules-conf}
    chmod 700 "$REH_WORK"
    trap rehearsal_cleanup EXIT
    CHECK_FAILS=0
    log "Rehearsal workdir: $REH_WORK (archive $(basename "$archive"))"

    docker network create --internal "$REHEARSAL_NET" >/dev/null
    "$SCRIPT_DIR/restore-drill.sh" "$archive" --keep --name acup-rehearsal-db \
        --network "$REHEARSAL_NET" --password-file "$REH_WORK/db.pw" | tee "$REH_WORK/drill.log"
    db_pw="$(cat "$REH_WORK/db.pw")"
    rmysql() { docker exec -i -e MYSQL_PWD="$db_pw" acup-rehearsal-db mysql -uroot -N -B "$@" 2>/dev/null; }
    chars_before="$(rmysql -e 'SELECT COUNT(*) FROM acore_characters.characters')"

    cp "$STACK_DIR/configs/modules/mod_ahbot.conf" "$REH_WORK/modules-conf/mod_ahbot.conf"
    refresh_module_confs "$REH_WORK/modules-conf" "$src"
    rehearsal_env "$REH_WORK/world.env" "$db_pw"

    # As the cutover does right before its db-import (same files, same guards).
    log "Applying the pre-db-import SQL to the restored copy..."
    if apply_pre_db_import_sql acup-rehearsal-db docker exec -i -e "MYSQL_PWD=${db_pw}" acup-rehearsal-db mysql -uroot -N -B \
            > "$REH_WORK/pre-db-import.log" 2>&1; then
        check_ok "pre-db-import SQL applied to the restored copy (every guard counts 0)"
    else
        check_fail "pre-db-import SQL failed (see $REH_WORK/pre-db-import.log); the candidate db-import was not run"
    fi

    if [ "$CHECK_FAILS" -eq 0 ]; then
        log "Running candidate db-import against the restored copy..."
        docker run -d --name acup-rehearsal-dbimport --network "$REHEARSAL_NET" --memory 2g \
            -e AC_DATA_DIR=/azerothcore/env/dist/data -e AC_LOGS_DIR=/azerothcore/env/dist/logs \
            -e "AC_LOGIN_DATABASE_INFO=acup-rehearsal-db;3306;root;${db_pw};acore_auth" \
            -e "AC_WORLD_DATABASE_INFO=acup-rehearsal-db;3306;root;${db_pw};acore_world" \
            -e "AC_CHARACTER_DATABASE_INFO=acup-rehearsal-db;3306;root;${db_pw};acore_characters" \
            -v "$REH_WORK/etc:/azerothcore/env/dist/etc" -v "$REH_WORK/logs:/azerothcore/env/dist/logs" \
            "acore/ac-wotlk-db-import:${CANDIDATE_TAG}" >/dev/null
        assert_rehearsal_isolated acup-rehearsal-dbimport
        rc="$(docker wait acup-rehearsal-dbimport)"
        docker logs acup-rehearsal-dbimport > "$REH_WORK/dbimport.log" 2>&1
        check_that "candidate db-import exited 0" "candidate db-import exited $rc (see $REH_WORK/dbimport.log)" [ "$rc" = 0 ]
    fi

    if [ "$CHECK_FAILS" -eq 0 ]; then
        log "Booting candidate worldserver (${REHEARSAL_BOTS} random bots)..."
        docker run -d -t -i --name acup-rehearsal-world --network "$REHEARSAL_NET" --memory 5g --cpus 4 \
            --env-file "$REH_WORK/world.env" \
            -v "$REH_WORK/etc:/azerothcore/env/dist/etc" -v "$REH_WORK/logs:/azerothcore/env/dist/logs" \
            -v "$REH_WORK/modules-conf:/azerothcore/env/dist/etc/modules" \
            -v "$src/modules:/azerothcore/modules:ro" \
            -v "$(stage_get CLIENT_DATA_TARGET_VOLUME | grep . || client_data_volume):/azerothcore/env/dist/data:ro" \
            "acore/ac-wotlk-worldserver:${CANDIDATE_TAG}" >/dev/null
        assert_rehearsal_isolated acup-rehearsal-world
        started="$(date +%s)"
        if wait_world_initialized acup-rehearsal-world "$WORLD_INIT_TIMEOUT"; then
            check_ok "candidate worldserver reached World Initialized in $(( $(date +%s) - started ))s"
            sleep 120
            check_that "worldserver still running 2 min after init" "worldserver stopped after init" \
                [ "$(container_status acup-rehearsal-world)" = running ]
        else
            check_fail "candidate worldserver never reached World Initialized"
        fi
        docker logs acup-rehearsal-world > "$REH_WORK/worldserver.log" 2>&1 || true
        rehearsal_checks
        chars_after="$(rmysql -e 'SELECT COUNT(*) FROM acore_characters.characters')"
        check_that "character count unchanged (${chars_after})" "character count changed ${chars_before} -> ${chars_after}" \
            [ "$chars_before" = "$chars_after" ]
        # The cutover's stop step requires exit code 0 after SIGTERM (a complete
        # final saveall): prove the candidate gives it before the window.
        if [ "$(container_status acup-rehearsal-world)" = running ]; then
            docker stop -t "$STOP_TIMEOUT" acup-rehearsal-world >/dev/null 2>&1 || true
            code="$(docker inspect -f '{{.State.ExitCode}}' acup-rehearsal-world 2>/dev/null || echo unknown)"
            check_that "candidate worldserver exited 0 on SIGTERM" \
                "candidate worldserver did not exit cleanly on SIGTERM (code ${code}); the cutover's stop step would refuse" \
                [ "$code" = 0 ]
        fi
    fi
    rehearsal_cleanup
    trap - EXIT
    if [ "$CHECK_FAILS" -eq 0 ]; then
        # What exactly passed: the cutover accepts it only for this build and pin set.
        {
            echo "REHEARSAL_OK=1"
            echo "CANDIDATE_TAG=$CANDIDATE_TAG"
            echo "BUILD_FINGERPRINT=$(stage_get BUILD_FINGERPRINT)"
            for svc in "${IMAGES[@]}"; do
                echo "CANDIDATE_ID_${svc//-/_}=$(stage_get "CANDIDATE_ID_${svc//-/_}")"
            done
            printf '%s\n' ${PINS[@]+"${PINS[@]}"} | { grep . || true; } | LC_ALL=C sort | sed 's/^/PIN=/'
        } > "$REH_WORK/result.env"
        ln -sfn "$REH_WORK" "$UPGRADE_ROOT/rehearsal-latest"
        log "Rehearsal PASSED: $REH_WORK"
    else
        log "Rehearsal FAILED (${CHECK_FAILS} check(s)); logs in $REH_WORK"
        exit 1
    fi
}

rehearsal_checks() {
    local n
    n="$(grep -c 'Missing property' "$REH_WORK/worldserver.log" || true)"
    check_that "no 'Missing property' config warnings" \
        "${n} 'Missing property' warnings: $(grep -o "Missing property [^ ]*" "$REH_WORK/worldserver.log" | sort -u | head -5 | tr '\n' ' ')" \
        [ "$n" = 0 ]
    n="$(grep -c 'built with generator' "$REH_WORK/worldserver.log" || true)"
    check_that "no stale-mmap generator errors" "${n} mmap generator-version errors (client data mismatch)" [ "$n" = 0 ]
    n="$(rmysql -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='acore_playerbots' AND table_name='playerbots_speech'")"
    check_that "mod-playerbots DB updates applied (playerbots_speech dropped)" "mod-playerbots DB updates did not run" [ "$n" = 0 ]
    n="$(rmysql -e "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='acore_world' AND table_name='creature' AND column_name='id'")"
    check_that "core world DB updates applied (creature.id present)" "creature.id missing: core world updates did not apply" [ "$n" = 1 ]
    python3 -I - "$REH_WORK/overlay-keys.txt" "$REH_WORK/etc/worldserver.conf" "$REH_WORK/modules-conf" \
        "$REPO_DIR/wow-server-sp-admin/app/services/env_var.py" > "$REH_WORK/unmapped.txt" <<'PY'
import importlib.util
import re
import sys
from pathlib import Path

keys_file, world_conf, module_dir, env_var_py = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("env_var", env_var_py)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
key_line = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.-]*)\s*=")
known = set()
for conf in [Path(world_conf), *sorted(Path(module_dir).glob("*.conf"))]:
    for line in conf.read_text(encoding="utf-8", errors="replace").splitlines():
        m = key_line.match(line)
        if m:
            known.add(module.config_key_to_ac_env_var(m.group(1)))
ignored = {"AC_DATA_DIR", "AC_LOGS_DIR", "AC_TEMP_DIR"}
keys = [line.strip() for line in open(keys_file, encoding="utf-8") if line.startswith("AC_")]
unmapped = sorted(
    key for key in keys
    if key not in known and key not in ignored and not key.endswith("_DATABASE_INFO")
)
for name in unmapped:
    print(f"  unmapped: {name}")
PY
    check_that "every AC_* override maps to a loaded config key" \
        "unmapped AC_* overrides: $(tr '\n' ' ' < "$REH_WORK/unmapped.txt")" [ ! -s "$REH_WORK/unmapped.txt" ]
    if [ -s "$REH_WORK/logs/Errors.log" ]; then
        check_info "Errors.log has $(wc -l < "$REH_WORK/logs/Errors.log") line(s); first: $(head -n 1 "$REH_WORK/logs/Errors.log")"
    else
        check_ok "Errors.log is empty"
    fi
}

# refresh_module_confs CONF_DIR SRC_ROOT — install the new module templates.
# playerbots/individualProgression .conf files carry no local edits (preflight
# proves it), so they are replaced by the new template; mod_ahbot.conf keeps
# the installer-written AuctionHouseBot.GUIDs and only its .dist is refreshed.
refresh_module_confs() {
    local conf_dir="$1" src="$2" spec module name dist
    for spec in "${MODULE_CONFS[@]}"; do
        module="${spec%%:*}"; name="${spec#*:}"
        dist="$src/modules/$module/conf/$name.conf.dist"
        [ -f "$dist" ] || [ "$DRY_RUN" = true ] || die "missing template $dist"
        run cp "$dist" "$conf_dir/$name.conf.dist"
        if [ "$name" != mod_ahbot ]; then
            run cp "$dist" "$conf_dir/$name.conf"
        fi
    done
}

# ---------------------------------------------------------------- cutover ---
# compare_counts — accounts and characters at the cold backup vs after the
# upgraded boot. A decrease is data loss: stop the realm. An increase can be
# legitimate (a player or the bot pool created one during boot): warn loudly,
# record COUNTS_WARNING and go on. An unreadable count stops the realm.
compare_counts() {
    local before=() after=() i name b a
    read -r -a before < "$STATE_DIR/counts-before.txt" || true
    read -r -a after < "$STATE_DIR/counts-after.txt" || true
    for i in 0 1; do
        if [ "$i" = 0 ]; then name=accounts; else name=characters; fi
        b="${before[$i]:-}"; a="${after[$i]:-}"
        if ! [[ "$b" =~ ^[0-9]+$ && "$a" =~ ^[0-9]+$ ]]; then
            stop_realm_and_die "cannot compare $name counts ('$b' -> '$a'; see $STATE_DIR/counts-before.txt and counts-after.txt)."
        elif [ "$a" -lt "$b" ]; then
            stop_realm_and_die "$name count DECREASED across the upgrade: $b -> $a ($((a - b))); data may have been lost."
        elif [ "$a" -gt "$b" ]; then
            echo "WARNING: $name $b -> $a (+$((a - b))) since the cold backup — expected only if a player or the bot pool created one during boot; check before opening the realm" >&2
            state_set COUNTS_WARNING "$name $b -> $a (+$((a - b)))"
        else
            log "  $name: $a (unchanged since the cold backup)"
        fi
    done
}

# refuse_realm_started_since_stop — azerothcore.service starts the stack at
# boot: a realm (or db-import) up between the stop step and BOOT_STARTED, or
# started since the stop step and stopped again, means a host reboot or a hand
# start ran the half-switched stack. The databases may have changed; never
# stop it and go on. Anything but exited/created/missing counts as up (a
# crash-looping container reports "restarting").
refuse_realm_started_since_stop() {
    local name status started_at started_epoch stopped_at
    stopped_at="$(state_get STOPPED_AT)"
    for name in ac-worldserver ac-authserver ac-db-import; do
        status="$(container_status "$name")"
        case "$status" in
            exited|created|missing) ;;
            *) die "the realm was restarted after the cutover stopped it ($name is $status; host reboot?); the databases may have been modified. Roll back: $0 rollback --state $STATE_DIR" ;;
        esac
        # Cutovers recorded before STOPPED_AT existed fall back to the status check.
        if [ -z "$stopped_at" ] || [ "$status" = missing ]; then continue; fi
        started_at="$(docker inspect -f '{{.State.StartedAt}}' "$name" 2>/dev/null || true)"
        started_epoch="$(date -u -d "$started_at" +%s 2>/dev/null)" \
            || die "cannot read when $name last started ('$started_at'); refusing to resume"
        if [ "$started_epoch" -gt "$stopped_at" ]; then
            die "the realm was restarted after the cutover stopped it ($name started at $started_at; host reboot?); the databases may have been modified. Roll back: $0 rollback --state $STATE_DIR"
        fi
    done
}

cmd_cutover() {
    local ts src label rel url branch rev svc vol scale archive key target_vol pin name recorded
    local problem code oom live_cmp live_note
    load_versions
    if [ -n "$STATE_DIR" ]; then
        resolve_state_dir
        refuse_rolled_back
        refuse_rollback_in_progress
        if [ "$(state_get CANDIDATE_TAG)" != "$CANDIDATE_TAG" ]; then
            die "pinned versions changed since this cutover started ($(state_get CANDIDATE_TAG) -> ${CANDIDATE_TAG}); resume with the tooling revision it started with"
        fi
        # The live checkouts get this run's patches: they must be the ones the
        # images were built with (cutovers recorded before the fingerprint skip this).
        if [ -n "$(state_get BUILD_FINGERPRINT)" ] && [ "$(state_get BUILD_FINGERPRINT)" != "$(build_fingerprint)" ]; then
            die "pinned versions or local patches changed since this cutover started (build fingerprint differs); resume with the tooling revision it started with"
        fi
        while IFS='|' read -r label rel url branch rev <&3; do
            if [ "$(state_get "PIN_REV_${label//-/_}")" != "$rev" ]; then
                die "pinned versions changed since this cutover started ($label: $(state_get "PIN_REV_${label//-/_}") -> $rev); resume with the tooling revision it started with"
            fi
        done 3< <(upstream_repo_specs)
        # The cutover's --pin values are part of its state: replay them, and
        # refuse a resume that names different ones.
        mapfile -t recorded < <(recorded_pins)
        if [ "${#PINS[@]}" -gt 0 ] && [ "$(printf '%s\n' "${PINS[@]}" | sort)" != "$(printf '%s\n' ${recorded[@]+"${recorded[@]}"} | sort)" ]; then
            die "pins differ from the cutover's (recorded: ${recorded[*]:-none}); resume without --pin to reuse them"
        fi
        PIN_ARGS=()
        for pin in ${recorded[@]+"${recorded[@]}"}; do PIN_ARGS+=(--pin "$pin"); done
        # After BOOT_STARTED a running realm is the cutover's own target stack
        # (e.g. an interrupted boot wait); the re-run boot step re-checks
        # db-import and World Initialized.
        if step_done stop && ! started BOOT_STARTED; then
            refuse_realm_started_since_stop
        fi
        if in_nightly_backup_window; then
            die "02:30-03:45 UTC overlaps the 03:00 nightly backup; resume outside that window (nothing was changed)"
        fi
        ADMIN_PRESENT="$(state_get ADMIN_PRESENT)"
        ADMIN_PRESENT="${ADMIN_PRESENT:-true}"
        # A host reboot also starts the admin app (scheduled restarts, console
        # writes, presence polling): keep it stopped until the admin step.
        if step_done stop && ! started ADMIN_STEP_STARTED && [ "$(container_status azerothcore-admin)" = running ]; then
            log "The admin app is running again (host reboot?); stopping it until the cutover's admin step"
            run docker stop azerothcore-admin
        fi
        log "Resuming cutover in $STATE_DIR"
    else
        problem="$(unfinished_current_cutover)" || die "$problem"
        preflight_checks cutover || die "preflight failed; nothing was changed"
        require_stage
        if [ "$SKIP_REHEARSAL" != true ]; then
            problem="$(rehearsal_problem)" \
                || die "no passing rehearsal for ${CANDIDATE_TAG} with this build and these --pin values (${problem}); run 'rehearse' (or pass --skip-rehearsal)"
        fi
        require_stage_matches_pins
        # Again here, not only in stage: a file created after staging must be
        # caught before the downtime starts.
        check_live_switch_collisions || die "local files collide with the pinned revisions; nothing was changed"
        # The client-data switch deletes the only live copy of the old data:
        # its rollback copy must be complete before any downtime is spent.
        if [ -n "$(stage_get CLIENT_DATA_TARGET_VOLUME)" ]; then
            problem="$(rollback_copy_problem "$(stage_get CLIENT_DATA_ROLLBACK_VOLUME)" "$(stage_get CLIENT_DATA_LIVE)" \
                "acore/ac-wotlk-client-data:$(env_value DOCKER_IMAGE_TAG)" "$(client_data_volume)")" \
                || die "client-data rollback copy: ${problem}; re-run 'stage' (it copies an incomplete volume again). Nothing was changed."
        fi
        ADMIN_PRESENT=false
        if [ -n "$(image_id azerothcore-admin:local)" ] && [ -f "$ADMIN_STACK_DIR/docker-compose.yml" ]; then
            ADMIN_PRESENT=true
        fi
        echo ""
        echo "This stops the realm, takes a cold backup, switches code/images/configs to"
        echo "core ${AC_CORE_REV:0:12} and boots it. Players are disconnected for the duration."
        confirm UPGRADE
        ts="$(date -u +%Y%m%dT%H%M%SZ)"
        STATE_DIR="$UPGRADE_ROOT/cutover-$ts"
        ensure_root
        run mkdir -p "$STATE_DIR"/{configs,etc,logs,patches-live,admin-stack}
        run chmod 700 "$STATE_DIR"
        [ "$DRY_RUN" = true ] || : > "$STATE_DIR/state.env"
        state_set TS "$ts"
        state_set CANDIDATE_TAG "$CANDIDATE_TAG"
        state_set PREVIOUS_REPO_REV "$PREVIOUS_REPO_REV"
        state_set ADMIN_PRESENT "$ADMIN_PRESENT"
        # Finalize deletes this staging tree; a resume refuses other inputs.
        state_set STAGING_SRC "$(stage_get STAGING_SRC | grep . || staging_src)"
        state_set BUILD_FINGERPRINT "$(stage_get BUILD_FINGERPRINT)"
        while IFS='|' read -r label rel url branch rev <&3; do
            state_set "OLD_REV_${label//-/_}" "$(git -C "$STACK_DIR/$rel" rev-parse HEAD)"
            state_set "PIN_REV_${label//-/_}" "$rev"
        done 3< <(upstream_repo_specs)
        for svc in "${IMAGES[@]}"; do
            state_set "CANDIDATE_ID_${svc//-/_}" "$(stage_get "CANDIDATE_ID_${svc//-/_}")"
        done
        for pin in ${PINS[@]+"${PINS[@]}"}; do state_set PIN "$pin"; done
        # Rollback and finalize read these from the state, never from the
        # staging file (a later 'stage' may rewrite or remove it).
        for key in "${CLIENT_DATA_FACTS[@]}"; do
            state_set "$key" "$(stage_get "$key")"
        done
        run ln -sfn "$STATE_DIR" "$UPGRADE_ROOT/current"
    fi
    trap cutover_exit_hint EXIT
    ts="$(state_get TS)"; ts="${ts:-dry-run}"
    src="$(staging_src)"

    if ! step_done snapshot_tooling; then
        log "Saving the previous repository's tooling for rollback verification"
        run mkdir -p "$STATE_DIR/repo-snapshot"
        if [ "$DRY_RUN" != true ]; then
            git -C "$REPO_DIR" archive "$(state_get PREVIOUS_REPO_REV)" scripts wow-server-sp-admin | tar -x -C "$STATE_DIR/repo-snapshot"
        fi
        mark_done snapshot_tooling
    fi

    if ! step_done stop; then
        # First attempt only: a resumed stop step finds the admin already stopped.
        if [ -z "$(state_get ADMIN_WAS_RUNNING)" ]; then
            state_set ADMIN_WAS_RUNNING "$([ "$(container_status azerothcore-admin)" = running ] && echo true || echo false)"
            state_set ADMIN_IMAGE_ID "$(image_id azerothcore-admin:local)"
        fi
        if [ "$ADMIN_PRESENT" = true ]; then
            run docker tag azerothcore-admin:local "azerothcore-admin:preupgrade-$ts"
            run cp -p "$ADMIN_STACK_DIR/docker-compose.yml" "$STATE_DIR/admin-stack/docker-compose.yml"
        else
            log "The admin app is not installed (no azerothcore-admin:local image or $ADMIN_STACK_DIR/docker-compose.yml): no admin rollback copy"
        fi
        log "Stopping the admin app (no scheduled jobs or console writes during cutover)"
        if [ "$(container_status azerothcore-admin)" = running ]; then
            run docker stop azerothcore-admin
            if [ "$ADMIN_PRESENT" != true ]; then
                log "Stopped a stray azerothcore-admin container (the admin app is not installed): it is left stopped after the cutover"
            fi
        fi
        log "Graceful worldserver stop (up to ${STOP_TIMEOUT}s for saveall), then authserver"
        run compose stop -t "$STOP_TIMEOUT" ac-worldserver
        run compose stop ac-authserver
        if [ "$DRY_RUN" != true ]; then
            [ "$(container_status ac-worldserver)" = exited ] || die "ac-worldserver did not stop cleanly"
            # A SIGKILL after the stop timeout (or an OOM kill) cut the final
            # saveall short: the cold backup would miss it.
            code="$(docker inspect -f '{{.State.ExitCode}}' ac-worldserver 2>/dev/null || echo unknown)"
            oom="$(docker inspect -f '{{.State.OOMKilled}}' ac-worldserver 2>/dev/null || echo unknown)"
            if [ "$code" != 0 ] || [ "$oom" != false ]; then
                die "ac-worldserver did not exit cleanly (code ${code}$([ "$oom" = false ] || echo ", OOMKilled=${oom}")); its last saveall may be incomplete. Start the realm again and resume (the stop step is repeated), or roll back: $0 rollback --state $STATE_DIR"
            fi
        fi
        # Resume evidence: a realm container started after this moment ran the
        # half-switched stack (see refuse_realm_started_since_stop).
        state_set STOPPED_AT "$(date -u +%s)"
        mark_done stop
    fi

    if ! step_done backup; then
        log "Cold pre-upgrade backup"
        run "$STACK_DIR/backup.sh" --label preupgrade
        if [ "$DRY_RUN" != true ]; then
            archive="$(find "$STACK_DIR/backups" -maxdepth 1 -name 'azerothcore-backup-preupgrade-*.tar.gz' -newer "$STATE_DIR/state.env" | sort | tail -n 1)"
            [ -n "$archive" ] || die "pre-upgrade archive not found"
            cp -p "$archive" "$STATE_DIR/"
            (cd "$STATE_DIR" && sha256sum "$(basename "$archive")" > "$(basename "$archive").sha256")
            state_set PREUPGRADE_ARCHIVE "$STATE_DIR/$(basename "$archive")"
            "$SCRIPT_DIR/restore-drill.sh" "$STATE_DIR/$(basename "$archive")" | tee "$STATE_DIR/logs/preupgrade-drill.log"
            db_query "SELECT (SELECT COUNT(*) FROM acore_auth.account),(SELECT COUNT(*) FROM acore_characters.characters)" \
                | tr '\t' ' ' > "$STATE_DIR/counts-before.txt"
        fi
        mark_done backup
    fi

    if ! step_done snapshot_state; then
        log "Capturing rollback state"
        run cp -p "$STACK_DIR/docker-compose.override.yml" "$STATE_DIR/docker-compose.override.yml"
        if [ -f "$STACK_DIR/docker-compose.admin.yml" ]; then
            run cp -p "$STACK_DIR/docker-compose.admin.yml" "$STATE_DIR/docker-compose.admin.yml"
        fi
        run cp -p "$STACK_DIR/.env" "$STATE_DIR/env"
        run cp -a "$STACK_DIR/configs/." "$STATE_DIR/configs/"
        for name in "${ETC_CONFS[@]}"; do
            for f in "$STACK_DIR/env/dist/etc/$name.conf" "$STACK_DIR/env/dist/etc/$name.conf.dist"; do
                if [ -f "$f" ]; then run cp -p "$f" "$STATE_DIR/etc/"; fi
            done
        done
        for svc in "${IMAGES[@]}"; do
            state_set "OLD_ID_${svc//-/_}" "$(image_id "acore/ac-wotlk-${svc}:$(env_value DOCKER_IMAGE_TAG)")"
            run docker tag "acore/ac-wotlk-${svc}:$(env_value DOCKER_IMAGE_TAG)" "acore/ac-wotlk-${svc}:preupgrade-$ts"
        done
        while IFS='|' read -r label rel url branch rev <&3; do
            run git -C "$STACK_DIR/$rel" tag -f "preupgrade-$ts" HEAD
            if [ "$DRY_RUN" != true ]; then
                git -C "$STACK_DIR/$rel" diff > "$STATE_DIR/patches-live/${label}.diff"
            fi
        done 3< <(upstream_repo_specs)
        mark_done snapshot_state
    fi

    if ! step_done switch_code; then
        log "Switching live checkouts to the pinned revisions"
        # From here on the host-mounted ./modules may hold new SQL that a
        # (re)started worldserver applies: rollback must restore the databases.
        mark_started SWITCH_CODE_STARTED
        while IFS='|' read -r label rel url branch rev <&3; do
            if [ "$label" != core ]; then run git -C "$STACK_DIR/$rel" checkout -q -- .; fi
            if [ "$DRY_RUN" = true ]; then
                run checkout_pinned_rev "$STACK_DIR/$rel" "$url" "$branch" "$rev"
            else
                checkout_pinned_rev "$STACK_DIR/$rel" "$url" "$branch" "$rev"
            fi
        done 3< <(upstream_repo_specs)
        run "$SCRIPT_DIR/apply-local-patches.sh" "$STACK_DIR"
        mark_done switch_code
    fi

    if ! step_done switch_config; then
        log "Refreshing config templates and migrating AC_* names"
        for name in "${ETC_CONFS[@]}"; do
            run rm -f "$STACK_DIR/env/dist/etc/$name.conf" "$STACK_DIR/env/dist/etc/$name.conf.dist"
        done
        refresh_module_confs "$STACK_DIR/configs/modules" "$STACK_DIR"
        run "$SCRIPT_DIR/migrate-ac-env.sh" --stack-dir "$STACK_DIR" --backup-dir "$STATE_DIR/pre-migrate" \
            ${PIN_ARGS[@]+"${PIN_ARGS[@]}"} \
            --validate-against "$STACK_DIR/src/server/apps/worldserver/worldserver.conf.dist" \
            --validate-against "$STACK_DIR/modules/mod-playerbots/conf/playerbots.conf.dist" \
            --validate-against "$STACK_DIR/modules/mod-individual-progression/conf/individualProgression.conf.dist" \
            --validate-against "$STACK_DIR/modules/mod-ah-bot-plus/conf/mod_ahbot.conf.dist"
        mark_done switch_config
    fi

    if ! step_done switch_client_data; then
        vol="$(client_data_volume)"
        target_vol="$(cutover_fact CLIENT_DATA_TARGET_VOLUME)"
        if [ -n "$target_vol" ]; then
            # The delete below is undone only from the rollback copy: verify it
            # again right before. Once a switch began the live volume is partly
            # the new data, so only the copy's own marker can vouch for it.
            live_cmp="$vol"; live_note="the live client data was not touched"
            if started CLIENT_DATA_SWITCH_STARTED; then
                live_cmp=""; live_note="the live client data is already partly switched (an earlier attempt started the switch)"
            fi
            # Probed with the live image, as at stage and before the confirm:
            # the images switch only after this step.
            problem="$(rollback_copy_problem "$(cutover_fact CLIENT_DATA_ROLLBACK_VOLUME)" "$(cutover_fact CLIENT_DATA_LIVE)" \
                "acore/ac-wotlk-client-data:$(env_value DOCKER_IMAGE_TAG)" ${live_cmp:+"$live_cmp"})" \
                || die "client-data rollback copy: ${problem}; ${live_note}"
            log "Installing client data $(cutover_fact CLIENT_DATA_TARGET) into $vol"
            mark_started CLIENT_DATA_SWITCH_STARTED
            run docker run --rm --network none --user 0 -v "$target_vol:/src:ro" -v "$vol:/dst" \
                --entrypoint sh "acore/ac-wotlk-client-data:${CANDIDATE_TAG}" \
                -c "test -f /src/data-version || { echo 'ERROR: source volume has no data-version; client data left untouched' >&2; exit 1; }; find /dst -mindepth 1 -delete && cp -a /src/. /dst/ && chown -R $(env_value DOCKER_USER_ID):$(env_value DOCKER_GROUP_ID) /dst"
        fi
        mark_done switch_client_data
    fi

    if ! step_done switch_images; then
        if [ "$DRY_RUN" != true ]; then
            for svc in "${IMAGES[@]}"; do
                if [ "$(image_id "acore/ac-wotlk-${svc}:${CANDIDATE_TAG}")" != "$(state_get "CANDIDATE_ID_${svc//-/_}")" ]; then
                    die "candidate images changed since the cutover started (acore/ac-wotlk-${svc}:${CANDIDATE_TAG}); roll back, then re-stage"
                fi
            done
        fi
        mark_started IMAGES_SWITCH_STARTED
        for svc in "${IMAGES[@]}"; do
            run docker tag "acore/ac-wotlk-${svc}:${CANDIDATE_TAG}" "acore/ac-wotlk-${svc}:$(env_value DOCKER_IMAGE_TAG)"
        done
        mark_done switch_images
    fi

    # From the boot to the end of verification, an unexpected failure (a count
    # query, a step marker) must stop the realm like a failed check does: never
    # half-up on an unverified version.
    set -E
    trap 'boot_failure_trap boot' ERR
    if ! step_done boot; then
        log "Validating the merged Compose configuration"
        run compose config -q
        mapfile -t scale < <(compose_scale_args)
        # From here on the databases change (pre-db-import SQL, then db-import):
        # rollback must restore them.
        mark_started BOOT_STARTED
        # ac-database kept running since the stop step. Idempotent: a resumed
        # boot step applies the files again (also after db-import ran).
        log "Applying the pre-db-import SQL to ac-database"
        # The $ expands in the container's shell (its root password), not here.
        # shellcheck disable=SC2016
        if ! apply_pre_db_import_sql ac-database \
                docker exec -i ac-database sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B "$@"' sh; then
            stop_realm_and_die "the pre-db-import SQL did not apply cleanly (see the error above); the stack was not started."
        fi
        # Flushed before the stack starts: the state says the SQL completed.
        state_mark PRE_DB_IMPORT_APPLIED
        log "Starting the stack (db-import applies all pending database updates first)"
        if ! run compose up -d --no-build ${scale[@]+"${scale[@]}"}; then
            docker logs ac-db-import > "$STATE_DIR/logs/db-import.log" 2>&1 || true
            stop_realm_and_die "stack start failed (db-import log: $STATE_DIR/logs/db-import.log)."
        fi
        if [ "$DRY_RUN" != true ]; then
            docker logs ac-db-import > "$STATE_DIR/logs/db-import.log" 2>&1 || true
            if [ "$(docker inspect -f '{{.State.ExitCode}}' ac-db-import)" != 0 ]; then
                stop_realm_and_die "ac-db-import did not exit 0 (log: $STATE_DIR/logs/db-import.log)."
            fi
            if ! wait_world_initialized ac-worldserver "$WORLD_INIT_TIMEOUT"; then
                docker logs ac-worldserver > "$STATE_DIR/logs/worldserver-boot.log" 2>&1 || true
                stop_realm_and_die "worldserver did not initialize (log: $STATE_DIR/logs/worldserver-boot.log)."
            fi
            docker logs ac-worldserver > "$STATE_DIR/logs/worldserver-boot.log" 2>&1 || true
        fi
        mark_done boot
    fi

    trap 'boot_failure_trap verify' ERR
    if ! step_done verify; then
        # A resumed verify finds the realm stopped by the failed attempt: start it
        # (a no-op when it is running) and wait for this boot before verifying.
        log "Starting the realm for verification (no-op when it is already up)"
        mapfile -t scale < <(compose_scale_args)
        if ! run compose up -d --no-build ${scale[@]+"${scale[@]}"}; then
            stop_realm_and_die "stack start before verification failed."
        fi
        log "Verifying the upgraded stack"
        if [ "$DRY_RUN" != true ]; then
            if ! wait_world_initialized ac-worldserver "$WORLD_INIT_TIMEOUT"; then
                docker logs ac-worldserver > "$STATE_DIR/logs/worldserver-boot.log" 2>&1 || true
                stop_realm_and_die "worldserver did not initialize before verification (log: $STATE_DIR/logs/worldserver-boot.log)."
            fi
            if ! "$SCRIPT_DIR/verify-azerothcore.sh" | tee "$STATE_DIR/logs/verify.log"; then
                stop_realm_and_die "verification failed (see $STATE_DIR/logs/verify.log)."
            fi
            db_query "SELECT (SELECT COUNT(*) FROM acore_auth.account),(SELECT COUNT(*) FROM acore_characters.characters)" \
                | tr '\t' ' ' > "$STATE_DIR/counts-after.txt"
            compare_counts
        fi
        mark_done verify
    fi
    trap - ERR
    set +E

    if ! step_done admin; then
        if [ "$ADMIN_PRESENT" = true ]; then
            log "Redeploying the admin app against the upgraded server"
            mark_started ADMIN_STEP_STARTED
            run docker start azerothcore-admin
            run "$REPO_DIR/wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh"
        else
            log "The admin app is not installed; nothing to redeploy"
            if [ "$(state_get ADMIN_WAS_RUNNING)" = true ]; then
                log "The stray azerothcore-admin container stopped at the stop step is left stopped"
            fi
        fi
        mark_done admin
    fi
    state_mark CUTOVER_OK
    if [ -n "$(state_get COUNTS_WARNING)" ]; then
        echo "WARNING: account/character counts increased during the upgrade (COUNTS_WARNING in $STATE_DIR/state.env); check them before opening the realm." >&2
    fi
    log "Cutover complete. Rollback stays available: $0 rollback --state $STATE_DIR"
    log "After a stable week: $0 finalize --state $STATE_DIR"
}

# --------------------------------------------------------------- rollback ---
# Rollback undoes only what the cutover did. The STEP_ markers and the started
# markers (set before each step's first mutation) say what may have changed;
# every artifact the undo needs is checked before the first mutation.
# rollback_needs_db_restore — true as soon as anything the old or new binaries
# could act on changed: the code switch (a restarted worldserver applies the
# switched ./modules SQL), the image switch, or the boot (db-import).
rollback_needs_db_restore() {
    step_done switch_code || started SWITCH_CODE_STARTED || started IMAGES_SWITCH_STARTED || started BOOT_STARTED
}

# rollback_restores_client_data — the client-data copy may have begun.
rollback_restores_client_data() {
    started CLIENT_DATA_SWITCH_STARTED && [ -n "$(state_get CLIENT_DATA_ROLLBACK_VOLUME)" ]
}

# rollback_preconditions TS
rollback_preconditions() {
    local ts="$1" label rel url branch rev svc vol problem old hits fails=0
    while IFS='|' read -r label rel url branch rev <&3; do
        old="$(state_get "OLD_REV_${label//-/_}")"
        [ -n "$old" ] || die "no recorded pre-upgrade revision for $label in $STATE_DIR"
        # The checkout back to the old revision must not fail (or overwrite a
        # local file) after the realm is stopped: prove it first.
        if ! hits="$(live_switch_collisions "$STACK_DIR/$rel" "$old")"; then
            echo "  $label: cannot compare $STACK_DIR/$rel with ${old:0:12}" >&2
            fails=$((fails + 1))
        elif [ -n "$hits" ]; then
            echo "  $label: local file(s) in $STACK_DIR/$rel collide with paths tracked at ${old:0:12}:" >&2
            sed -n '1,20s/^/    /p' <<< "$hits" >&2
            fails=$((fails + 1))
        fi
    done 3< <(upstream_repo_specs)
    if [ "$fails" -gt 0 ]; then
        die "local files collide with the pre-upgrade revisions (listed above); move them, then re-run the rollback. Nothing was changed."
    fi
    if step_done snapshot_state; then
        if [ ! -f "$STATE_DIR/docker-compose.override.yml" ] || [ ! -d "$STATE_DIR/configs" ]; then
            die "configuration copies missing from $STATE_DIR"
        fi
        for svc in "${IMAGES[@]}"; do
            [ -n "$(image_id "acore/ac-wotlk-${svc}:preupgrade-$ts")" ] \
                || die "rollback image acore/ac-wotlk-${svc}:preupgrade-$ts is missing"
        done
    fi
    if started CLIENT_DATA_SWITCH_STARTED && [ -z "$(state_get CLIENT_DATA_ROLLBACK_VOLUME)" ]; then
        die "client data was switched but no rollback volume is recorded in $STATE_DIR"
    fi
    # `docker run -v` would create a missing volume empty, and the copy would
    # then empty the live client data. An empty or partial copy (an interrupted
    # stage) must be caught before the realm stops, not by the copy's own guard
    # afterwards. The live volume may hold the new data now: the copy's
    # completion marker is the reference.
    if rollback_restores_client_data; then
        vol="$(state_get CLIENT_DATA_ROLLBACK_VOLUME)"
        problem="$(rollback_copy_problem "$vol" "$(state_get CLIENT_DATA_LIVE)" "acore/ac-wotlk-client-data:preupgrade-$ts")" \
            || die "client-data rollback volume: ${problem}; restore it before rolling back"
    fi
    if started ADMIN_STEP_STARTED; then
        [ -f "$STATE_DIR/admin-stack/docker-compose.yml" ] || die "admin compose file copy missing from $STATE_DIR"
        [ -n "$(image_id "azerothcore-admin:preupgrade-$ts")" ] || die "rollback image azerothcore-admin:preupgrade-$ts is missing"
    fi
}

# save_rollback_diff LABEL DIR — keep whatever a discard would lose (e.g. edits
# made after the cutover) in $STATE_DIR/patches-rollback/; never overwrites.
save_rollback_diff() {
    local label="$1" dir="$2" diff out
    diff="$(git -C "$dir" diff)"
    [ -n "$diff" ] || return 0
    out="$STATE_DIR/patches-rollback/${label}.diff"
    [ -e "$out" ] && out="$STATE_DIR/patches-rollback/${label}.$(date -u +%Y%m%dT%H%M%SZ).diff"
    if [ "$DRY_RUN" = true ]; then
        log "  would save the $label working-tree diff to $out"
        return 0
    fi
    mkdir -p "$STATE_DIR/patches-rollback"
    printf '%s\n' "$diff" > "$out"
    log "  saved the $label working-tree diff to $out before discarding it"
}

# take_prerollback_copy — after go-live the rollback is about to drop the
# upgraded databases and overwrite post-cutover configs: save both first
# (once per cutover) in $STATE_DIR/pre-rollback/, so the upgraded state stays
# reachable. The realm is stopped, so the archive is consistent.
take_prerollback_copy() {
    local dir="$STATE_DIR/pre-rollback" name f before archive
    log "Saving the upgraded state before the rollback overwrites it: $dir"
    run mkdir -p "$dir/configs" "$dir/etc"
    for f in "$STACK_DIR/docker-compose.override.yml" "$STACK_DIR/docker-compose.admin.yml"; do
        if [ -f "$f" ]; then run cp -p "$f" "$dir/"; fi
    done
    run cp -a "$STACK_DIR/configs/." "$dir/configs/"
    for name in "${ETC_CONFS[@]}"; do
        for f in "$STACK_DIR/env/dist/etc/$name.conf" "$STACK_DIR/env/dist/etc/$name.conf.dist"; do
            if [ -f "$f" ]; then run cp -p "$f" "$dir/etc/"; fi
        done
    done
    if [ "$NO_SAFETY_BACKUP" = true ]; then
        echo "WARNING: --no-safety-backup: the upgraded databases are NOT saved before the restore drops them." >&2
    else
        before="$({ find "$STACK_DIR/backups" -maxdepth 1 -name 'azerothcore-backup-prerollback-*.tar.gz' 2>/dev/null || true; } | sort)"
        if ! run "$STACK_DIR/backup.sh" --label prerollback; then
            die "the pre-rollback safety backup failed; nothing was overwritten and the realm stays stopped. Fix the cause and re-run: $0 rollback --state $STATE_DIR — or add --no-safety-backup to roll back without saving the upgraded databases"
        fi
        if [ "$DRY_RUN" != true ]; then
            archive="$(comm -13 <(printf '%s\n' "$before") \
                <(find "$STACK_DIR/backups" -maxdepth 1 -name 'azerothcore-backup-prerollback-*.tar.gz' | sort) | tail -n 1)"
            [ -n "$archive" ] || die "the pre-rollback archive was not found in $STACK_DIR/backups; nothing was overwritten"
            cp -p "$archive" "$dir/"
            (cd "$dir" && sha256sum "$(basename "$archive")" > "$(basename "$archive").sha256")
            state_set PRE_ROLLBACK_ARCHIVE "$dir/$(basename "$archive")"
            log "  upgraded databases saved: $dir/$(basename "$archive")"
        fi
    fi
    state_mark PRE_ROLLBACK_BACKUP
}

# rollback_verify — wait for this boot, then judge it with the previous
# repository's verifier; only a pass (or the operator's explicit acceptance)
# closes the rollback.
rollback_verify() {
    local verifier="$STATE_DIR/repo-snapshot/scripts/verify-azerothcore.sh"
    # The old verifier needs a fully booted realm.
    if [ "$DRY_RUN" != true ] && ! wait_world_initialized ac-worldserver "$WORLD_INIT_TIMEOUT"; then
        die "ac-worldserver did not reach World Initialized after the rollback (see docker logs ac-worldserver)"
    fi
    if [ -f "$verifier" ]; then
        log "Verifying with the previous repository's verifier"
        if [ "$DRY_RUN" != true ]; then
            "$verifier" | tee "$STATE_DIR/logs/rollback-verify.log" \
                || die "rollback verification failed; see $STATE_DIR/logs/rollback-verify.log"
        fi
    else
        echo "WARNING: no tooling snapshot in $STATE_DIR (the cutover stopped before snapshot_tooling);" \
            "verify by hand with verify-azerothcore.sh from $(state_get PREVIOUS_REPO_REV)." >&2
    fi
    state_mark ROLLED_BACK
    log "Rollback complete. The repository checkout still holds the new tooling: check out ${PREVIOUS_REPO_REV:-$(state_get PREVIOUS_REPO_REV)} before using the installer or redeploy scripts."
}

cmd_rollback() {
    local ts label rel url branch rev old svc vol archive tag rollback_vol scale admin_was_up name f problem
    resolve_state_dir
    refuse_rolled_back
    load_versions
    if [ "$(state_get ROLLBACK_RESTORED)" = 1 ]; then
        # Code, configs, client data, images, databases and admin are back and
        # the stack was started: only the verification is open. Never restore twice.
        trap rollback_exit_hint EXIT
        if [ "$ACCEPT_VERIFY_FAILURE" = true ]; then
            {
                echo "WARNING: --accept-verify-failure: closing this rollback although the previous repository's"
                echo "WARNING: verifier did not pass (see $STATE_DIR/logs/rollback-verify.log). This is your judgment;"
                echo "WARNING: the failed checks stay unresolved."
            } >&2
            state_mark ROLLED_BACK
            log "Rollback recorded as complete (verification failure accepted)."
            return 0
        fi
        log "The stack was already restored; re-running only the wait and the previous verifier"
        rollback_verify
        return 0
    fi
    if [ "$ACCEPT_VERIFY_FAILURE" = true ]; then
        die "--accept-verify-failure only closes a rollback that already restored the stack (ROLLBACK_RESTORED=1 in $STATE_DIR); run the rollback first"
    fi
    ts="$(state_get TS)"
    tag="$(env_value DOCKER_IMAGE_TAG)"
    archive="$(state_get PREUPGRADE_ARCHIVE)"
    if [ -n "$archive" ]; then
        # Verify the recorded rollback point before any mutation, and restore
        # exactly the file that was verified.
        archive="$STATE_DIR/$(basename "$archive")"
        [ -f "$archive" ] || die "pre-upgrade archive $archive is missing"
        (cd "$STATE_DIR" && sha256sum -c --quiet "$(basename "$archive").sha256") || die "pre-upgrade archive checksum mismatch"
    elif rollback_needs_db_restore; then
        die "databases may have been migrated but no pre-upgrade archive was recorded in $STATE_DIR; restore a backup by hand"
    fi
    rollback_preconditions "$ts"
    echo "Rolling back to the pre-upgrade state captured in $STATE_DIR."
    if rollback_needs_db_restore; then
        echo "The databases are restored from $(basename "$archive"): everything players did after the cutover's backup is lost."
    else
        echo "The live code was never switched: the databases were not touched and are kept."
    fi
    confirm ROLLBACK
    # From here the stack is being returned: a resume or finalize must not interleave.
    state_mark ROLLBACK_STARTED
    trap rollback_exit_hint EXIT

    log "Stopping the admin app and the realm"
    admin_was_up=false
    if [ "$(container_status azerothcore-admin)" = running ]; then
        admin_was_up=true
        run docker stop azerothcore-admin
    fi
    run compose stop -t "$STOP_TIMEOUT" ac-worldserver
    run compose stop ac-authserver

    if rollback_needs_db_restore && started BOOT_STARTED && [ "$(state_get PRE_ROLLBACK_BACKUP)" != 1 ]; then
        take_prerollback_copy
    fi

    log "Restoring source checkouts"
    while IFS='|' read -r label rel url branch rev <&3; do
        old="$(state_get "OLD_REV_${label//-/_}")"
        # Tracked changes (core and modules) are discarded only once
        # snapshot_state saved them (patches-live/); before that the code was
        # never switched.
        if step_done snapshot_state; then
            save_rollback_diff "$label" "$STACK_DIR/$rel"
            run git -C "$STACK_DIR/$rel" checkout -q -- .
        fi
        run git -C "$STACK_DIR/$rel" -c advice.detachedHead=false checkout -q -B "$branch" "$old"
        # Re-apply the pre-upgrade working-tree diff unless the tree already
        # holds it (a tree that was never discarded, e.g. a re-run).
        if [ -s "$STATE_DIR/patches-live/${label}.diff" ]; then
            if git -C "$STACK_DIR/$rel" apply --reverse --check "$STATE_DIR/patches-live/${label}.diff" 2>/dev/null; then
                log "  $label: the pre-upgrade working-tree diff is already applied"
            else
                run git -C "$STACK_DIR/$rel" apply "$STATE_DIR/patches-live/${label}.diff"
            fi
        fi
    done 3< <(upstream_repo_specs)

    if step_done snapshot_state; then
        log "Restoring configuration files"
        run cp -p "$STATE_DIR/docker-compose.override.yml" "$STACK_DIR/docker-compose.override.yml"
        if [ -f "$STATE_DIR/docker-compose.admin.yml" ]; then
            # In place: the admin container bind-mounts this file.
            if [ "$DRY_RUN" = true ]; then run cat "$STATE_DIR/docker-compose.admin.yml"; else cat "$STATE_DIR/docker-compose.admin.yml" > "$STACK_DIR/docker-compose.admin.yml"; fi
        fi
        run cp -a "$STATE_DIR/configs/." "$STACK_DIR/configs/"
        for name in "${ETC_CONFS[@]}"; do
            for f in "$STATE_DIR/etc/$name.conf" "$STATE_DIR/etc/$name.conf.dist"; do
                if [ -f "$f" ]; then run cp -p "$f" "$STACK_DIR/env/dist/etc/"; fi
            done
        done
    else
        log "Configs untouched (cutover stopped before snapshot_state)"
    fi

    vol="$(client_data_volume)"
    rollback_vol="$(state_get CLIENT_DATA_ROLLBACK_VOLUME)"
    if rollback_restores_client_data; then
        log "Restoring client data $(state_get CLIENT_DATA_LIVE) from $rollback_vol"
        run docker run --rm --network none --user 0 -v "$rollback_vol:/src:ro" -v "$vol:/dst" \
            --entrypoint sh "acore/ac-wotlk-client-data:preupgrade-$ts" \
            -c 'test -f /src/data-version || { echo "ERROR: source volume has no data-version; client data left untouched" >&2; exit 1; }; find /dst -mindepth 1 -delete && cp -a /src/. /dst/ && rm -f /dst/.acup-complete'
        # The copy is only as good as what arrived: the live volume must now
        # hold the pre-upgrade version with exactly the copy's marked totals.
        if [ "$DRY_RUN" != true ]; then
            problem="$(restored_client_data_problem "$vol" "$rollback_vol" "$(state_get CLIENT_DATA_LIVE)" \
                "acore/ac-wotlk-client-data:preupgrade-$ts")" \
                || die "the restored client data does not match its rollback copy: ${problem}. $rollback_vol was only read, so it is safe to re-run: $0 rollback --state $STATE_DIR"
        fi
    else
        log "Client data untouched"
    fi

    if step_done snapshot_state; then
        log "Restoring pre-upgrade images"
        for svc in "${IMAGES[@]}"; do
            run docker tag "acore/ac-wotlk-${svc}:preupgrade-$ts" "acore/ac-wotlk-${svc}:${tag}"
            if [ "$DRY_RUN" != true ] && [ "$(image_id "acore/ac-wotlk-${svc}:${tag}")" != "$(state_get "OLD_ID_${svc//-/_}")" ]; then
                die "acore/ac-wotlk-${svc}:${tag} is not the recorded pre-upgrade image after retagging"
            fi
        done
    else
        log "Images untouched (cutover stopped before snapshot_state)"
    fi

    if rollback_needs_db_restore; then
        log "Restoring the databases from $(basename "$archive")"
        # Same machine: the archive's realmlist is the right one, and a re-run
        # after the databases were dropped has no fresh one to read.
        RESTORE_READY_TIMEOUT_SECONDS="$WORLD_INIT_TIMEOUT" \
            run "$SCRIPT_DIR/restore-azerothcore.sh" "$archive" --stack-dir "$STACK_DIR" --yes --keep-archive-realmlist
    else
        log "Databases untouched (the live code was never switched)"
    fi
    mapfile -t scale < <(compose_scale_args)
    run compose up -d --no-build ${scale[@]+"${scale[@]}"}

    if started ADMIN_STEP_STARTED; then
        log "Restoring the previous admin app image"
        run docker tag "azerothcore-admin:preupgrade-$ts" azerothcore-admin:local
        run cp -p "$STATE_DIR/admin-stack/docker-compose.yml" "$ADMIN_STACK_DIR/docker-compose.yml"
        if [ "$DRY_RUN" = true ]; then
            run docker compose --env-file "$ADMIN_STACK_DIR/.env" up -d --force-recreate
        else
            (cd "$ADMIN_STACK_DIR" && docker compose --env-file .env up -d --force-recreate)
        fi
    elif [ "$(state_get ADMIN_WAS_RUNNING)" = true ] || [ "$admin_was_up" = true ]; then
        log "Starting the admin app again (it was only stopped)"
        run docker start azerothcore-admin
    elif [ "$(state_get ADMIN_PRESENT)" = false ]; then
        log "The admin app is not installed; nothing to restore"
    else
        log "Admin app left as it was"
    fi
    # Everything is back and started; from here a re-run only verifies.
    state_mark ROLLBACK_RESTORED
    rollback_verify
}

# --------------------------------------------------------------- finalize ---
cmd_finalize() {
    local ts label rel url branch rev svc staging
    resolve_state_dir
    load_versions
    [ "$(state_get CUTOVER_OK)" = 1 ] || die "cutover in $STATE_DIR did not complete; refusing to delete rollback artifacts"
    if [ "$(state_get ROLLED_BACK)" = 1 ]; then die "this upgrade was rolled back; nothing to finalize"; fi
    refuse_rollback_in_progress
    ts="$(state_get TS)"
    echo "This deletes the rollback images, client-data copies, git tags and staging checkout"
    echo "for the upgrade in $STATE_DIR. The pre-upgrade archive in $STATE_DIR is kept."
    confirm FINALIZE
    for svc in "${IMAGES[@]}"; do
        run docker image rm "acore/ac-wotlk-${svc}:preupgrade-$ts" || true
        run docker image rm "acore/ac-wotlk-${svc}:$(state_get CANDIDATE_TAG)" || true
    done
    run docker image rm "azerothcore-admin:preupgrade-$ts" || true
    for vol in "$(state_get CLIENT_DATA_ROLLBACK_VOLUME)" "$(state_get CLIENT_DATA_TARGET_VOLUME)"; do
        # Re-runnable: a volume a previous finalize already removed is skipped.
        if [ -n "$vol" ] && docker volume inspect "$vol" >/dev/null 2>&1; then run docker volume rm "$vol"; fi
    done
    while IFS='|' read -r label rel url branch rev <&3; do
        run git -C "$STACK_DIR/$rel" tag -d "preupgrade-$ts" || true
    done 3< <(upstream_repo_specs)
    # The staging tree this cutover used, never one derived from the current
    # pins (they may have moved on); only ever inside the staging root.
    staging="$(state_get STAGING_SRC)"
    if [ -z "$staging" ] && [ -n "$(state_get CANDIDATE_TAG)" ]; then
        staging="$UPGRADE_ROOT/staging/src-$(state_get CANDIDATE_TAG | sed 's/^upgrade-//')"
    fi
    case "$staging" in
        */..|*/../*) ;;
        "$UPGRADE_ROOT"/staging/src-?*) run rm -rf -- "$staging"; staging="" ;;
    esac
    if [ -n "$staging" ]; then
        echo "WARNING: not deleting the recorded staging checkout '$staging' (outside $UPGRADE_ROOT/staging); remove it by hand." >&2
    fi
    state_mark FINALIZED
    log "Finalized. Move or delete $STATE_DIR (it holds the pre-upgrade archive) when you no longer need it."
}

cmd_status() {
    load_versions
    echo "Pinned: core ${AC_CORE_REV:0:12}, playerbots ${MOD_PLAYERBOTS_REV:0:12}, client data ${CLIENT_DATA_VERSION}"
    if [ -f "$(stage_env)" ]; then echo "Stage:  $(stage_env)"; else echo "Stage:  none"; fi
    echo "Rehearsal: $(grep -s '^REHEARSAL_OK=' "$UPGRADE_ROOT/rehearsal-latest/result.env" >/dev/null && readlink -f "$UPGRADE_ROOT/rehearsal-latest" || echo none)"
    if [ -L "$UPGRADE_ROOT/current" ]; then
        STATE_DIR="$(readlink -f "$UPGRADE_ROOT/current")"
        echo "Cutover: $STATE_DIR"
        { grep -E '^(STEP_|[A-Z_]+_STARTED=|PRE_DB_IMPORT_APPLIED|CUTOVER_OK|COUNTS_WARNING|ROLLBACK_RESTORED|ROLLED_BACK|FINALIZED)' "$STATE_DIR/state.env" || true; } | sed 's/^/  /'
    else
        echo "Cutover: none"
    fi
}

case "$CMD" in
    preflight) cmd_preflight ;;
    stage) take_lock; cmd_stage ;;
    rehearse) take_lock; cmd_rehearse ;;
    cutover) take_lock; cmd_cutover ;;
    rollback) take_lock; cmd_rollback ;;
    finalize) take_lock; cmd_finalize ;;
    status) cmd_status ;;
    *) usage ;;
esac
