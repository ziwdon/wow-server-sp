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
#   rollback   return to the exact pre-cutover state (code, images, configs, DB)
#   finalize   delete rollback artifacts after the upgrade has proven itself
#   status     show the current upgrade state
#
# Usage:
#   upgrade-azerothcore.sh preflight --previous-repo-rev REV
#   upgrade-azerothcore.sh stage
#   upgrade-azerothcore.sh rehearse [--archive FILE]
#   upgrade-azerothcore.sh cutover --previous-repo-rev REV [--pin AC_K=V]... [--yes]
#                                  [--state DIR] [--skip-rehearsal] [--allow-online-players]
#   upgrade-azerothcore.sh rollback [--state DIR] [--yes]
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
        --previous-repo-rev) PREVIOUS_REPO_REV="${2:?}"; shift 2 ;;
        --state) STATE_DIR="${2:?}"; shift 2 ;;
        --archive) ARCHIVE="${2:?}"; shift 2 ;;
        --pin) PINS+=("${2:?}"); shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1" >&2; usage ;;
    esac
done

PIN_ARGS=()
for pin in ${PINS[@]+"${PINS[@]}"}; do PIN_ARGS+=(--pin "$pin"); done

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
    while [ "$(date +%s)" -lt "$deadline" ]; do
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

image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }

state_set() {
    [ "$DRY_RUN" = true ] && return 0
    printf '%s=%s\n' "$1" "$2" >> "$STATE_DIR/state.env"
}
state_get() {
    [ -f "$STATE_DIR/state.env" ] || return 0
    { grep -E "^$1=" "$STATE_DIR/state.env" || true; } | tail -n 1 | cut -d= -f2-
}
step_done() { [ "$(state_get "STEP_$1")" = 1 ]; }
mark_done() { state_set "STEP_$1" 1; }
# Started markers (CLIENT_DATA_SWITCH_STARTED, IMAGES_SWITCH_STARTED,
# BOOT_STARTED, ADMIN_STEP_STARTED) are written immediately BEFORE a step's
# first mutation, so rollback knows what may have changed even when the step
# died before its STEP_ marker.
mark_started() { state_set "$1" 1; }
started() { [ "$(state_get "$1")" = 1 ]; }

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
    while IFS='|' read -r label rel url branch rev; do
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
    done < <(upstream_repo_specs)
    if [ "$fails" -gt 0 ]; then
        echo "  Move or remove the files listed above (or commit them upstream), then re-run."
        return 1
    fi
}

# -------------------------------------------------------------- preflight ---
preflight_checks() {
    local mode="$1" label rel url branch rev dir free_gb name now_min humans
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
    while IFS='|' read -r label rel url branch rev; do
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
    done < <(upstream_repo_specs)
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
        now_min=$(( 10#$(date -u +%H) * 60 + 10#$(date -u +%M) ))
        if [ "$now_min" -ge 150 ] && [ "$now_min" -lt 225 ]; then
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

stage_get() {
    { grep -E "^$1=" "$(stage_env)" 2>/dev/null || true; } | tail -n 1 | cut -d= -f2-
}

cmd_stage() {
    local src label rel url branch rev uid gid svc vol live_ver rollback_vol target_vol
    load_versions
    src="$(staging_src)"
    ensure_root
    run mkdir -p "$UPGRADE_ROOT/staging"

    log "Staging checkout at pinned revisions: $src"
    while IFS='|' read -r label rel url branch rev; do
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
    done < <(upstream_repo_specs)
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
    live_ver="$(volume_data_version "$vol" "acore/ac-wotlk-client-data:$(env_value DOCKER_IMAGE_TAG)")"
    rollback_vol="acup-client-data-rollback-${live_ver:-unknown}"
    target_vol="acup-client-data-${CLIENT_DATA_VERSION}"
    if [ "$live_ver" != "$CLIENT_DATA_VERSION" ]; then
        log "Client data changes ${live_ver:-unknown} -> ${CLIENT_DATA_VERSION}."
        if ! docker volume inspect "$rollback_vol" >/dev/null 2>&1; then
            log "  copying live client data to rollback volume $rollback_vol (read-only source)"
            run docker volume create "$rollback_vol" >/dev/null
            run docker run --rm --network none --user 0 -v "$vol:/src:ro" -v "$rollback_vol:/dst" \
                --entrypoint sh "acore/ac-wotlk-client-data:$(env_value DOCKER_IMAGE_TAG)" -c 'cp -a /src/. /dst/'
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
    (cd "$src" && docker compose --env-file "$STACK_DIR/.env" -f docker-compose.yml \
        -f "$REH_WORK/override.yml" -f "$REH_WORK/admin.yml" config --format json) > "$REH_WORK/compose.json"
    python3 -I - "$REH_WORK/compose.json" "$src/conf/dist/env.ac" "$password" "$REHEARSAL_BOTS" > "$out" <<'PY'
import json
import sys

compose, env_ac, password, bots = sys.argv[1:5]
env = {}
for line in open(env_ac, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        env[key] = value
service = json.load(open(compose, encoding="utf-8"))["services"]["ac-worldserver"]
raw = service.get("environment") or {}
if isinstance(raw, list):
    raw = dict(item.split("=", 1) for item in raw)
env.update({k: "" if v is None else str(v) for k, v in raw.items()})
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
    local src mem_gb archive started rc db_pw chars_before chars_after
    load_versions
    require_stage
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
        docker stop -t "$STOP_TIMEOUT" acup-rehearsal-world >/dev/null 2>&1 || true
    fi
    rehearsal_cleanup
    trap - EXIT
    if [ "$CHECK_FAILS" -eq 0 ]; then
        printf 'REHEARSAL_OK=1\nCANDIDATE_TAG=%s\nCANDIDATE_ID_worldserver=%s\n' \
            "$CANDIDATE_TAG" "$(stage_get CANDIDATE_ID_worldserver)" > "$REH_WORK/result.env"
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
    python3 -I - "$REH_WORK/world.env" "$REH_WORK/etc/worldserver.conf" "$REH_WORK/modules-conf" \
        "$REPO_DIR/wow-server-sp-admin/app/services/env_var.py" > "$REH_WORK/unmapped.txt" <<'PY'
import importlib.util
import re
import sys
from pathlib import Path

env_file, world_conf, module_dir, env_var_py = sys.argv[1:5]
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
unmapped = sorted(
    line.split("=", 1)[0] for line in open(env_file, encoding="utf-8")
    if line.startswith("AC_") and line.split("=", 1)[0] not in known
    and line.split("=", 1)[0] not in ignored and not line.split("=", 1)[0].endswith("_DATABASE_INFO")
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
cmd_cutover() {
    local ts src label rel url branch rev svc vol scale archive key target_vol previous_state
    load_versions
    if [ -n "$STATE_DIR" ]; then
        resolve_state_dir
        refuse_rolled_back
        if [ "$(state_get CANDIDATE_TAG)" != "$CANDIDATE_TAG" ]; then
            die "pinned versions changed since this cutover started ($(state_get CANDIDATE_TAG) -> ${CANDIDATE_TAG}); resume with the tooling revision it started with"
        fi
        log "Resuming cutover in $STATE_DIR"
    else
        if [ -L "$UPGRADE_ROOT/current" ]; then
            previous_state="$(readlink -f "$UPGRADE_ROOT/current")"
            if [ -f "$previous_state/state.env" ] && ! grep -qxE '(CUTOVER_OK|ROLLED_BACK)=1' "$previous_state/state.env"; then
                die "the cutover in $previous_state is incomplete; resume it ($0 cutover --state $previous_state) or roll it back ($0 rollback --state $previous_state)"
            fi
        fi
        preflight_checks cutover || die "preflight failed; nothing was changed"
        require_stage
        if [ "$SKIP_REHEARSAL" != true ]; then
            [ "$(grep -s '^CANDIDATE_TAG=' "$UPGRADE_ROOT/rehearsal-latest/result.env" | cut -d= -f2)" = "$CANDIDATE_TAG" ] \
                || die "no passing rehearsal for ${CANDIDATE_TAG}; run 'rehearse' (or pass --skip-rehearsal)"
        fi
        # Again here, not only in stage: a file created after staging must be
        # caught before the downtime starts.
        check_live_switch_collisions || die "local files collide with the pinned revisions; nothing was changed"
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
        while IFS='|' read -r label rel url branch rev; do
            state_set "OLD_REV_${label//-/_}" "$(git -C "$STACK_DIR/$rel" rev-parse HEAD)"
        done < <(upstream_repo_specs)
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
        run docker tag azerothcore-admin:local "azerothcore-admin:preupgrade-$ts"
        run cp -p "$ADMIN_STACK_DIR/docker-compose.yml" "$STATE_DIR/admin-stack/docker-compose.yml"
        log "Stopping the admin app (no scheduled jobs or console writes during cutover)"
        if [ "$(container_status azerothcore-admin)" = running ]; then run docker stop azerothcore-admin; fi
        log "Graceful worldserver stop (up to ${STOP_TIMEOUT}s for saveall), then authserver"
        run compose stop -t "$STOP_TIMEOUT" ac-worldserver
        run compose stop ac-authserver
        if [ "$DRY_RUN" != true ]; then
            [ "$(container_status ac-worldserver)" = exited ] || die "ac-worldserver did not stop cleanly"
        fi
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
        while IFS='|' read -r label rel url branch rev; do
            run git -C "$STACK_DIR/$rel" tag -f "preupgrade-$ts" HEAD
            if [ "$DRY_RUN" != true ]; then
                git -C "$STACK_DIR/$rel" diff > "$STATE_DIR/patches-live/${label}.diff"
            fi
        done < <(upstream_repo_specs)
        mark_done snapshot_state
    fi

    if ! step_done switch_code; then
        log "Switching live checkouts to the pinned revisions"
        while IFS='|' read -r label rel url branch rev; do
            if [ "$label" != core ]; then run git -C "$STACK_DIR/$rel" checkout -q -- .; fi
            if [ "$DRY_RUN" = true ]; then
                run checkout_pinned_rev "$STACK_DIR/$rel" "$url" "$branch" "$rev"
            else
                checkout_pinned_rev "$STACK_DIR/$rel" "$url" "$branch" "$rev"
            fi
        done < <(upstream_repo_specs)
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
            log "Installing client data $(cutover_fact CLIENT_DATA_TARGET) into $vol"
            mark_started CLIENT_DATA_SWITCH_STARTED
            run docker run --rm --network none --user 0 -v "$target_vol:/src:ro" -v "$vol:/dst" \
                --entrypoint sh "acore/ac-wotlk-client-data:${CANDIDATE_TAG}" \
                -c "find /dst -mindepth 1 -delete && cp -a /src/. /dst/ && chown -R $(env_value DOCKER_USER_ID):$(env_value DOCKER_GROUP_ID) /dst"
        fi
        mark_done switch_client_data
    fi

    if ! step_done switch_images; then
        mark_started IMAGES_SWITCH_STARTED
        for svc in "${IMAGES[@]}"; do
            run docker tag "acore/ac-wotlk-${svc}:${CANDIDATE_TAG}" "acore/ac-wotlk-${svc}:$(env_value DOCKER_IMAGE_TAG)"
        done
        mark_done switch_images
    fi

    if ! step_done boot; then
        log "Validating the merged Compose configuration"
        run compose config -q
        mapfile -t scale < <(compose_scale_args)
        log "Starting the stack (db-import applies all pending database updates first)"
        # From here on db-import may migrate the databases: rollback must restore them.
        mark_started BOOT_STARTED
        if ! run compose up -d --no-build ${scale[@]+"${scale[@]}"}; then
            docker logs ac-db-import > "$STATE_DIR/logs/db-import.log" 2>&1 || true
            run compose stop ac-worldserver ac-authserver || true
            die "stack start failed (db-import log: $STATE_DIR/logs/db-import.log). Server is stopped. Fix and re-run: $0 cutover --state $STATE_DIR  — or roll back: $0 rollback --state $STATE_DIR"
        fi
        if [ "$DRY_RUN" != true ]; then
            docker logs ac-db-import > "$STATE_DIR/logs/db-import.log" 2>&1 || true
            [ "$(docker inspect -f '{{.State.ExitCode}}' ac-db-import)" = 0 ] || die "ac-db-import did not exit 0"
            if ! wait_world_initialized ac-worldserver "$WORLD_INIT_TIMEOUT"; then
                docker logs ac-worldserver > "$STATE_DIR/logs/worldserver-boot.log" 2>&1 || true
                compose stop ac-worldserver ac-authserver || true
                die "worldserver did not initialize. Server is stopped. Rollback: $0 rollback --state $STATE_DIR"
            fi
            docker logs ac-worldserver > "$STATE_DIR/logs/worldserver-boot.log" 2>&1 || true
        fi
        mark_done boot
    fi

    if ! step_done verify; then
        log "Verifying the upgraded stack"
        if [ "$DRY_RUN" != true ]; then
            if ! "$SCRIPT_DIR/verify-azerothcore.sh" | tee "$STATE_DIR/logs/verify.log"; then
                compose stop ac-worldserver ac-authserver || true
                die "verification failed (see $STATE_DIR/logs/verify.log). Server is stopped. Fix and re-run: $0 cutover --state $STATE_DIR — or roll back: $0 rollback --state $STATE_DIR"
            fi
            db_query "SELECT (SELECT COUNT(*) FROM acore_auth.account),(SELECT COUNT(*) FROM acore_characters.characters)" \
                | tr '\t' ' ' > "$STATE_DIR/counts-after.txt"
            cmp -s "$STATE_DIR/counts-before.txt" "$STATE_DIR/counts-after.txt" \
                || die "account/character counts changed across the upgrade: $(cat "$STATE_DIR/counts-before.txt") -> $(cat "$STATE_DIR/counts-after.txt")"
        fi
        mark_done verify
    fi

    if ! step_done admin; then
        log "Redeploying the admin app against the upgraded server"
        mark_started ADMIN_STEP_STARTED
        run docker start azerothcore-admin
        run "$REPO_DIR/wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh"
        mark_done admin
    fi
    state_set CUTOVER_OK 1
    log "Cutover complete. Rollback stays available: $0 rollback --state $STATE_DIR"
    log "After a stable week: $0 finalize --state $STATE_DIR"
}

# --------------------------------------------------------------- rollback ---
# Rollback undoes only what the cutover did. The STEP_ markers and the started
# markers (set before each step's first mutation) say what may have changed;
# every artifact the undo needs is checked before the first mutation.
# rollback_preconditions TS
rollback_preconditions() {
    local ts="$1" label rel url branch rev svc
    while IFS='|' read -r label rel url branch rev; do
        [ -n "$(state_get "OLD_REV_${label//-/_}")" ] || die "no recorded pre-upgrade revision for $label in $STATE_DIR"
    done < <(upstream_repo_specs)
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
    if started ADMIN_STEP_STARTED; then
        [ -f "$STATE_DIR/admin-stack/docker-compose.yml" ] || die "admin compose file copy missing from $STATE_DIR"
        [ -n "$(image_id "azerothcore-admin:preupgrade-$ts")" ] || die "rollback image azerothcore-admin:preupgrade-$ts is missing"
    fi
}

cmd_rollback() {
    local ts label rel url branch rev old svc vol archive tag rollback_vol verifier scale admin_was_up
    resolve_state_dir
    refuse_rolled_back
    load_versions
    ts="$(state_get TS)"
    tag="$(env_value DOCKER_IMAGE_TAG)"
    archive="$(state_get PREUPGRADE_ARCHIVE)"
    if [ -n "$archive" ]; then
        # Verify the recorded rollback point before any mutation, and restore
        # exactly the file that was verified.
        archive="$STATE_DIR/$(basename "$archive")"
        [ -f "$archive" ] || die "pre-upgrade archive $archive is missing"
        (cd "$STATE_DIR" && sha256sum -c --quiet "$(basename "$archive").sha256") || die "pre-upgrade archive checksum mismatch"
    elif started BOOT_STARTED; then
        die "databases may have been migrated but no pre-upgrade archive was recorded in $STATE_DIR; restore a backup by hand"
    fi
    rollback_preconditions "$ts"
    echo "Rolling back to the pre-upgrade state captured in $STATE_DIR."
    if started BOOT_STARTED; then
        echo "The databases are restored from $(basename "$archive"): everything players did after the cutover's backup is lost."
    else
        echo "The new version never booted: the databases were not touched and are kept."
    fi
    confirm ROLLBACK

    log "Stopping the admin app and the realm"
    admin_was_up=false
    if [ "$(container_status azerothcore-admin)" = running ]; then
        admin_was_up=true
        run docker stop azerothcore-admin
    fi
    run compose stop -t "$STOP_TIMEOUT" ac-worldserver
    run compose stop ac-authserver

    log "Restoring source checkouts"
    while IFS='|' read -r label rel url branch rev; do
        old="$(state_get "OLD_REV_${label//-/_}")"
        # Tracked module changes are discarded only once snapshot_state saved
        # them (patches-live/); before that the code was never switched.
        if [ "$label" != core ] && step_done snapshot_state; then run git -C "$STACK_DIR/$rel" checkout -q -- .; fi
        run git -C "$STACK_DIR/$rel" -c advice.detachedHead=false checkout -q -B "$branch" "$old"
        if [ -s "$STATE_DIR/patches-live/${label}.diff" ]; then
            run git -C "$STACK_DIR/$rel" apply "$STATE_DIR/patches-live/${label}.diff"
        fi
    done < <(upstream_repo_specs)

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
    if started CLIENT_DATA_SWITCH_STARTED && [ -n "$rollback_vol" ]; then
        log "Restoring client data $(state_get CLIENT_DATA_LIVE) from $rollback_vol"
        run docker run --rm --network none --user 0 -v "$rollback_vol:/src:ro" -v "$vol:/dst" \
            --entrypoint sh "acore/ac-wotlk-client-data:preupgrade-$ts" -c 'find /dst -mindepth 1 -delete && cp -a /src/. /dst/'
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

    if started BOOT_STARTED; then
        log "Restoring the databases from $(basename "$archive")"
        run "$SCRIPT_DIR/restore-azerothcore.sh" "$archive" --stack-dir "$STACK_DIR" --yes
    else
        log "Databases untouched (the new version never booted)"
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
    else
        log "Admin app left as it was"
    fi

    verifier="$STATE_DIR/repo-snapshot/scripts/verify-azerothcore.sh"
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
    state_set ROLLED_BACK 1
    log "Rollback complete. The repository checkout still holds the new tooling: check out ${PREVIOUS_REPO_REV:-$(state_get PREVIOUS_REPO_REV)} before using the installer or redeploy scripts."
}

# --------------------------------------------------------------- finalize ---
cmd_finalize() {
    local ts label rel url branch rev svc
    resolve_state_dir
    load_versions
    [ "$(state_get CUTOVER_OK)" = 1 ] || die "cutover in $STATE_DIR did not complete; refusing to delete rollback artifacts"
    if [ "$(state_get ROLLED_BACK)" = 1 ]; then die "this upgrade was rolled back; nothing to finalize"; fi
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
    while IFS='|' read -r label rel url branch rev; do
        run git -C "$STACK_DIR/$rel" tag -d "preupgrade-$ts" || true
    done < <(upstream_repo_specs)
    run rm -rf -- "$(staging_src)"
    state_set FINALIZED 1
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
        { grep -E '^(STEP_|[A-Z_]+_STARTED=|CUTOVER_OK|ROLLED_BACK|FINALIZED)' "$STATE_DIR/state.env" || true; } | sed 's/^/  /'
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
