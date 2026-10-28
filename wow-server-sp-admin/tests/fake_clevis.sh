#!/bin/bash
# Test stand-in for `clevis` (no TPM in the sandbox): "sealing" is base64 with a FAKE: prefix.
#
#   fake_clevis.sh encrypt tpm2 '{}'   stdin -> FAKE:<base64>
#   fake_clevis.sh decrypt             FAKE:<base64> on stdin -> original bytes
#
# It refuses any other arguments, so a test also proves the vault asks for exactly
# `tpm2` with an empty config (no PCR policy, spec 6.3). When $FAKE_CLEVIS_FAIL is
# set it exits 1 like clevis does when the TPM cannot unseal.
if [ -n "${FAKE_CLEVIS_FAIL:-}" ]; then
    echo "fake clevis: failing on request" >&2
    exit 1
fi
case "${1:-}" in
    encrypt)
        if [ "${2:-}" != "tpm2" ] || [ "${3:-}" != "{}" ] || [ "$#" -ne 3 ]; then
            echo "fake clevis: expected: encrypt tpm2 '{}'" >&2
            exit 2
        fi
        printf 'FAKE:'
        base64 -w0
        echo
        ;;
    decrypt)
        if [ "$#" -ne 1 ]; then
            echo "fake clevis: expected: decrypt" >&2
            exit 2
        fi
        data=$(cat)
        case "$data" in
            FAKE:*) ;;
            *) echo "fake clevis: not a FAKE: token" >&2; exit 1 ;;
        esac
        printf '%s' "${data#FAKE:}" | base64 -d
        ;;
    *)
        echo "fake clevis: unknown command" >&2
        exit 2
        ;;
esac
