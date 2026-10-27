# shellcheck shell=bash
# ============================================================================
# scripts/lib/upstream-versions.sh — sourced, not executed.
# ----------------------------------------------------------------------------
# Reads scripts/upstream-versions.env (the pinned upstream revisions this
# repository's installer, verifier and admin app are written against) and
# checks out a git repository at an exact pinned commit.
#
# The versions file is parsed, never sourced: every line must be KEY=VALUE
# with a known key and a shell-safe value.
# ============================================================================

UPSTREAM_VERSION_KEYS=(
    AC_CORE_REPO AC_CORE_BRANCH AC_CORE_REV
    MOD_PLAYERBOTS_REPO MOD_PLAYERBOTS_BRANCH MOD_PLAYERBOTS_REV
    MOD_AHBOT_REPO MOD_AHBOT_BRANCH MOD_AHBOT_REV
    MOD_IP_REPO MOD_IP_BRANCH MOD_IP_REV
    CLIENT_DATA_VERSION
)

# load_upstream_versions FILE — sets every UPSTREAM_VERSION_KEYS variable.
load_upstream_versions() {
    local file="$1" line key value known
    if [ ! -f "$file" ]; then
        echo "ERROR: pinned versions file not found: $file" >&2
        return 1
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        if ! [[ "$line" =~ ^([A-Z0-9_]+)=([A-Za-z0-9._:/@+-]+)$ ]]; then
            echo "ERROR: malformed line in $file: $line" >&2
            return 1
        fi
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        known=false
        for k in "${UPSTREAM_VERSION_KEYS[@]}"; do
            [ "$k" = "$key" ] && known=true
        done
        if [ "$known" != true ]; then
            echo "ERROR: unknown key in $file: $key" >&2
            return 1
        fi
        printf -v "$key" '%s' "$value"
    done < "$file"
    for key in "${UPSTREAM_VERSION_KEYS[@]}"; do
        if [ -z "${!key:-}" ]; then
            echo "ERROR: $key is missing from $file" >&2
            return 1
        fi
    done
    for key in AC_CORE_REV MOD_PLAYERBOTS_REV MOD_AHBOT_REV MOD_IP_REV; do
        if ! [[ "${!key}" =~ ^[0-9a-f]{40}$ ]]; then
            echo "ERROR: $key must be a full 40-character commit SHA" >&2
            return 1
        fi
    done
}

# upstream_repo_specs — one "label|relative-dir|url|branch|rev" line per repo,
# core first. Call after load_upstream_versions.
upstream_repo_specs() {
    printf '%s\n' \
        "core|.|${AC_CORE_REPO}|${AC_CORE_BRANCH}|${AC_CORE_REV}" \
        "mod-playerbots|modules/mod-playerbots|${MOD_PLAYERBOTS_REPO}|${MOD_PLAYERBOTS_BRANCH}|${MOD_PLAYERBOTS_REV}" \
        "mod-ah-bot-plus|modules/mod-ah-bot-plus|${MOD_AHBOT_REPO}|${MOD_AHBOT_BRANCH}|${MOD_AHBOT_REV}" \
        "mod-individual-progression|modules/mod-individual-progression|${MOD_IP_REPO}|${MOD_IP_BRANCH}|${MOD_IP_REV}"
}

# checkout_pinned_rev DIR URL BRANCH REV — make DIR's BRANCH point at REV and
# check it out. Initialises DIR when it is not yet a repository. Fetches REV
# (depth 1) only when the commit is not already present. Refuses — via git's
# own checks — to overwrite local modifications to tracked files.
checkout_pinned_rev() {
    local dir="$1" url="$2" branch="$3" rev="$4"
    if [ ! -d "$dir/.git" ]; then
        mkdir -p "$dir"
        git -C "$dir" init -q
        git -C "$dir" remote add origin "$url"
    fi
    if ! git -C "$dir" cat-file -e "${rev}^{commit}" 2>/dev/null; then
        git -C "$dir" fetch -q --depth 1 origin "$rev"
    fi
    git -C "$dir" -c advice.detachedHead=false checkout -q -B "$branch" "$rev"
    if [ "$(git -C "$dir" rev-parse HEAD)" != "$rev" ]; then
        echo "ERROR: $dir is not at $rev after checkout" >&2
        return 1
    fi
}
