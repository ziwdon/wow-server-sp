#!/bin/bash
set -euo pipefail

# ============================================================================
# apply-local-patches.sh
# ----------------------------------------------------------------------------
# Apply this repository's local source patches to an AzerothCore tree.
# scripts/patches/<module>/*.patch applies to <source-root>/modules/<module>,
# in filename order.
#
# Usage: apply-local-patches.sh SOURCE_ROOT [--check | --reverse]
#   (default) apply every patch not yet applied; already-applied ones are kept
#   --check   report only; exit 1 if any patch is not applied
#   --reverse un-apply every applied patch (newest first)
# Exit: 0 ok, 1 a patch neither applies nor is applied (refresh it), 2 usage.
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_DIR="${PATCH_DIR:-${SCRIPT_DIR}/patches}"

usage() {
    sed -n '/^# Usage:/,/^# Exit:/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

[ $# -ge 1 ] || usage
SRC_ROOT="$1"
MODE="${2:-apply}"
case "$MODE" in
    apply|--check|--reverse) ;;
    *) usage ;;
esac
[ -d "$SRC_ROOT" ] || { echo "ERROR: source root not found: $SRC_ROOT" >&2; exit 1; }

shopt -s nullglob
patches=()
for patch in "$PATCH_DIR"/*/*.patch; do
    patches+=("$patch")
done
if [ "${#patches[@]}" -eq 0 ]; then
    echo "No local patches under $PATCH_DIR."
    exit 0
fi
if [ "$MODE" = --reverse ]; then
    mapfile -t patches < <(printf '%s\n' "${patches[@]}" | sort -r)
fi

status=0
for patch in "${patches[@]}"; do
    module="$(basename "$(dirname "$patch")")"
    target="${SRC_ROOT}/modules/${module}"
    name="${module}/$(basename "$patch")"
    if [ ! -e "$target/.git" ]; then
        echo "ERROR: ${name}: ${target} is not a git checkout" >&2
        status=1
        continue
    fi
    if git -C "$target" apply --reverse --check "$patch" 2>/dev/null; then
        if [ "$MODE" = --reverse ]; then
            git -C "$target" apply --reverse "$patch"
            echo "  - ${name} reverted"
        else
            echo "  = ${name} already applied"
        fi
    elif git -C "$target" apply --check "$patch" 2>/dev/null; then
        case "$MODE" in
            apply) git -C "$target" apply "$patch"; echo "  + ${name} applied" ;;
            --check) echo "  ! ${name} NOT applied"; status=1 ;;
            --reverse) echo "  = ${name} not applied; nothing to revert" ;;
        esac
    else
        echo "ERROR: ${name} neither applies nor is applied to ${target}; refresh the patch for this upstream revision." >&2
        status=1
    fi
done
exit "$status"
