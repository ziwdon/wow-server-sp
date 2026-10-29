"""scripts/tpm-env.sh: the sourced helper that maps the host TPM into the admin
container (spec section 6.3).

Everything here runs the real helper under bash inside the pytest sandbox, with
stub-only getent/id on PATH and TPM_DEVICE_PATH pointed at tmp files, fifos or
a harmless character device. Nothing touches /dev/tpmrm0 or any Docker daemon.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ADMIN_ROOT = Path(__file__).resolve().parents[1]
TPM_ENV = ADMIN_ROOT / "scripts/tpm-env.sh"
REDEPLOY = ADMIN_ROOT / "scripts/redeploy-azerothcore-admin.sh"

# A character device that is neither /dev/null (the "no TPM" fallback) nor the
# real TPM, so a test can tell "found a device" from "fell back".
CHAR_DEVICE = "/dev/zero"


def _stub(directory: Path, name: str, body: str) -> None:
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _bash(
    tmp_path: Path,
    snippet: str,
    *,
    strict: bool = False,
    getent: str | None = None,
    id_g: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source the helper and run `snippet`, optionally under the callers' `set -euo pipefail`."""
    stubs = tmp_path / "stubs"
    if getent is not None:
        _stub(stubs, "getent", getent)
    if id_g is not None:
        _stub(stubs, "id", f'[ "$1" = -g ] && echo {id_g} && exit 0\nexec /usr/bin/id "$@"\n')
    env = {k: v for k, v in os.environ.items() if k not in {"HOST_GID", "TPM_DEVICE_PATH"}}
    env["PATH"] = f"{stubs}:{env['PATH']}"
    env.update(extra_env or {})
    prefix = "set -euo pipefail\n" if strict else ""
    script = f'{prefix}source "{TPM_ENV}"\n{snippet}\n'
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=env, check=False)


TSS_PRESENT = 'echo "tss:x:4242:"\n'
TSS_ABSENT = "exit 2\n"


def test_helper_is_sourced_not_executed_and_quiet_when_sourced(tmp_path):
    lines = TPM_ENV.read_text().splitlines()
    header = "\n".join(lines[:4]).lower()
    assert "sourced" in header and "never executed" in header
    # Top-level code is only function definitions: no `set`, `exit`, or output.
    code = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    assert not any(line.startswith(("set ", "exit", "echo", "printf")) for line in code)

    result = _bash(tmp_path, 'echo "[${TPM_DEVICE-unset}][${TSS_GID-unset}]"')
    assert result.returncode == 0, result.stderr
    # Sourcing alone prints nothing and sets nothing; only the functions run.
    assert result.stdout == "[unset][unset]\n"
    assert result.stderr == ""


@pytest.mark.parametrize("kind", ["char_device", "regular_file", "fifo", "missing", "directory"])
def test_resolve_tpm_env_uses_device_only_when_it_is_a_character_device(tmp_path, kind):
    if kind == "char_device":
        path = CHAR_DEVICE
        expected = CHAR_DEVICE
    else:
        path = str(tmp_path / "tpmrm0")
        expected = "/dev/null"
        if kind == "regular_file":
            Path(path).write_text("")
        elif kind == "fifo":
            os.mkfifo(path)
        elif kind == "directory":
            Path(path).mkdir()

    result = _bash(
        tmp_path,
        'resolve_tpm_env; echo "$TPM_DEVICE"',
        strict=True,
        getent=TSS_PRESENT,
        extra_env={"TPM_DEVICE_PATH": path},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_resolve_tpm_env_defaults_to_the_host_tpm_path(tmp_path):
    # The default path is /dev/tpmrm0; on a machine without one this is the
    # /dev/null fallback, never an empty value (an empty host path would make
    # compose reject the mapping).
    result = _bash(tmp_path, 'resolve_tpm_env; echo "$TPM_DEVICE"', strict=True, getent=TSS_PRESENT)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() in {"/dev/tpmrm0", "/dev/null"}
    assert 'TPM_DEVICE_PATH:-/dev/tpmrm0' in TPM_ENV.read_text()


def test_resolve_tpm_env_tss_gid_comes_from_the_tss_group_first(tmp_path):
    result = _bash(
        tmp_path,
        'resolve_tpm_env; echo "$TSS_GID"',
        strict=True,
        getent=TSS_PRESENT,
        id_g="4321",
        extra_env={"HOST_GID": "777"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "4242"


def test_resolve_tpm_env_tss_gid_falls_back_to_host_gid_then_id_g(tmp_path):
    # `getent group tss` fails (no tss group) and the caller is under
    # `set -euo pipefail`: the helper must fall through, not abort the caller.
    host = _bash(
        tmp_path,
        'resolve_tpm_env; echo "$TSS_GID"',
        strict=True,
        getent=TSS_ABSENT,
        id_g="4321",
        extra_env={"HOST_GID": "777"},
    )
    assert host.returncode == 0, host.stderr
    assert host.stdout.strip() == "777"

    plain = _bash(
        tmp_path,
        'resolve_tpm_env; echo "$TSS_GID"',
        strict=True,
        getent=TSS_ABSENT,
        id_g="4321",
    )
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout.strip() == "4321"


def test_resolve_tpm_env_ignores_non_numeric_gids(tmp_path):
    result = _bash(
        tmp_path,
        'resolve_tpm_env; echo "$TSS_GID"',
        strict=True,
        getent='echo "tss:x::"\n',
        id_g="4321",
        extra_env={"HOST_GID": "not-a-number"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "4321"


def _env_file(tmp_path: Path, content: str, mode: int = 0o600) -> Path:
    path = tmp_path / ".env"
    path.write_text(content)
    path.chmod(mode)
    return path


BASE_ENV = "TAILSCALE_IP=100.64.0.1\nLAN_IP=\nADMIN_PORT=8765\nHOST_UID=1000\nHOST_GID=1000\nDOCKER_GID=999\n"


def test_backfill_appends_only_missing_keys_and_keeps_mode(tmp_path):
    env_file = _env_file(tmp_path, BASE_ENV)
    result = _bash(
        tmp_path,
        f'backfill_tpm_env "{env_file}"; echo "added=[$TPM_ENV_ADDED]"',
        strict=True,
        getent=TSS_PRESENT,
        extra_env={"TPM_DEVICE_PATH": CHAR_DEVICE},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "added=[TPM_DEVICE TSS_GID]"
    # Existing lines are untouched and in place; the two keys are appended.
    assert env_file.read_text() == BASE_ENV + f"TPM_DEVICE={CHAR_DEVICE}\nTSS_GID=4242\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_backfill_adds_only_the_key_that_is_missing(tmp_path):
    existing = BASE_ENV + "TPM_DEVICE=/dev/null\n"
    env_file = _env_file(tmp_path, existing)
    result = _bash(
        tmp_path,
        f'backfill_tpm_env "{env_file}"; echo "added=[$TPM_ENV_ADDED]"',
        strict=True,
        getent=TSS_PRESENT,
        extra_env={"TPM_DEVICE_PATH": CHAR_DEVICE},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "added=[TSS_GID]"
    # The operator's TPM_DEVICE=/dev/null (e.g. TPM deliberately off) is not "fixed".
    assert env_file.read_text() == existing + "TSS_GID=4242\n"


def test_backfill_leaves_a_complete_env_byte_identical(tmp_path):
    existing = BASE_ENV + "TPM_DEVICE=\nTSS_GID=116\n"  # an empty value still counts as set
    env_file = _env_file(tmp_path, existing)
    before = env_file.stat()
    result = _bash(
        tmp_path,
        f'backfill_tpm_env "{env_file}"; echo "added=[$TPM_ENV_ADDED]"',
        strict=True,
        getent=TSS_PRESENT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "added=[]"
    assert env_file.read_text() == existing
    assert env_file.stat().st_mtime_ns == before.st_mtime_ns


def test_backfill_does_not_treat_comments_or_prefixed_names_as_set(tmp_path):
    existing = BASE_ENV + "# TPM_DEVICE=/dev/tpmrm0\nOLD_TSS_GID=5\n"
    env_file = _env_file(tmp_path, existing)
    result = _bash(
        tmp_path,
        f'backfill_tpm_env "{env_file}"; echo "added=[$TPM_ENV_ADDED]"',
        strict=True,
        getent=TSS_PRESENT,
        extra_env={"TPM_DEVICE_PATH": CHAR_DEVICE},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "added=[TPM_DEVICE TSS_GID]"
    assert env_file.read_text().startswith(existing)


def test_backfill_starts_on_a_new_line_when_the_file_has_no_trailing_newline(tmp_path):
    existing = BASE_ENV.rstrip("\n")
    env_file = _env_file(tmp_path, existing)
    result = _bash(
        tmp_path,
        f'backfill_tpm_env "{env_file}"',
        strict=True,
        getent=TSS_PRESENT,
    )
    assert result.returncode == 0, result.stderr
    lines = env_file.read_text().splitlines()
    assert lines[: len(BASE_ENV.splitlines())] == BASE_ENV.splitlines()
    assert lines[-2].startswith("TPM_DEVICE=")
    assert lines[-1] == "TSS_GID=4242"


def test_backfill_warns_and_creates_nothing_when_the_env_file_is_missing(tmp_path):
    missing = tmp_path / "absent.env"
    result = _bash(
        tmp_path,
        f'rc=0; backfill_tpm_env "{missing}" || rc=$?; echo "rc=$rc"',
        strict=True,
        getent=TSS_PRESENT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rc=1"
    assert "WARNING" in result.stderr and str(missing) in result.stderr
    assert not missing.exists()


def test_redeploy_backfills_before_building_and_never_aborts_on_failure():
    text = REDEPLOY.read_text()
    lines = text.splitlines()
    source_at = next(i for i, line in enumerate(lines) if 'source "$SCRIPT_DIR/tpm-env.sh"' in line)
    assert "shellcheck disable=SC1091" in lines[source_at - 1]
    # The candidate build reads the .env, so the keys must exist before it.
    call = text.index('backfill_tpm_env "$STACK_DIR/.env"')
    assert call < text.index('echo "==> Building candidate image')
    # Cloud backup is optional: a .env that cannot be appended to must not stop
    # a redeploy (set -e + the ERR trap would otherwise fail it).
    call_line = next(line for line in lines if 'backfill_tpm_env "$STACK_DIR/.env"' in line)
    assert "||" in call_line or call_line.lstrip().startswith("if ")


def _redeploy_tpm_block(tmp_path: Path, stack: Path) -> Path:
    """The redeploy's TPM block, run alone under the script's own strictness."""
    text = REDEPLOY.read_text()
    marker = "# Cloud backup needs TPM_DEVICE/TSS_GID"
    block = text.split(marker, 1)[1].split('echo "==> Building candidate image', 1)[0]
    script = tmp_path / "redeploy-tpm-block.sh"
    script.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "trap 'echo \"Redeploy failed at line $LINENO.\" >&2' ERR\n"
        f'SCRIPT_DIR="{ADMIN_ROOT / "scripts"}"\n'
        f'STACK_DIR="{stack}"\n'
        f"{marker}{block}"
        'echo "REACHED BUILD"\n'
    )
    script.chmod(0o700)
    return script


def test_redeploy_block_logs_what_it_added_and_continues(tmp_path):
    stack = tmp_path / "stack"
    stack.mkdir()
    env_file = stack / ".env"
    env_file.write_text(BASE_ENV)
    env_file.chmod(0o600)
    stubs = tmp_path / "stubs"
    _stub(stubs, "getent", TSS_PRESENT)
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "TPM_DEVICE_PATH": CHAR_DEVICE}
    script = _redeploy_tpm_block(tmp_path, stack)

    first = subprocess.run([str(script)], text=True, capture_output=True, env=env, check=False)
    assert first.returncode == 0, first.stderr
    assert f"Added TPM_DEVICE TSS_GID to {env_file}" in first.stdout
    assert first.stdout.rstrip().endswith("REACHED BUILD")

    second = subprocess.run([str(script)], text=True, capture_output=True, env=env, check=False)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "REACHED BUILD"  # nothing to add, nothing printed
    assert env_file.read_text() == BASE_ENV + f"TPM_DEVICE={CHAR_DEVICE}\nTSS_GID=4242\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_redeploy_block_warns_but_does_not_abort_when_env_cannot_be_updated(tmp_path):
    stack = tmp_path / "stack"
    stack.mkdir()  # no .env: backfill refuses to create one
    script = _redeploy_tpm_block(tmp_path, stack)

    result = subprocess.run([str(script)], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "REACHED BUILD" in result.stdout
    assert "WARNING: could not add TPM settings" in result.stderr
    assert "Redeploy failed" not in result.stderr
    assert not (stack / ".env").exists()
