"""The TPM-sealed GPG vault that protects the Proton Drive session.

Runs real ``gpg`` and ``pass`` (a throwaway GNUPGHOME and password store per test)
and ``tests/fake_clevis.sh`` in place of ``clevis``, since the sandbox has no TPM.
The ``pass`` calls use the environment ``ProtonDriveCli.env()`` gives the real CLI,
so these tests also prove the vault's directories are the ones the CLI will use.

The fake clevis fails when ``$FAKE_CLEVIS_FAIL`` is set. The vault builds its
subprocess environments from scratch (it must not inherit the app's), so the
``make_vault`` fixture lets that one variable through ``clevis_env()`` on the test
instances only; the app code has no test hook.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.services import cloud_vault
from app.services.cloud_cli import CliPaths, ProtonDriveCli
from app.services.cloud_vault import SessionVault, VaultError

if shutil.which("gpg") is None or shutil.which("pass") is None or shutil.which("gpgconf") is None:
    pytest.skip("gpg, gpgconf and pass are needed for the vault tests", allow_module_level=True)

FAKE_CLEVIS = Path(__file__).parent / "fake_clevis.sh"
UNLOCK_MESSAGE = "Can't unlock the stored session — reconnect"


@pytest.fixture
def make_vault(tmp_path):
    """Build vaults under a short directory (a gpg-agent socket path is limited to 108 bytes)."""
    base = Path(tempfile.mkdtemp(prefix="wsp-vault-"))
    clevis = tmp_path / "clevis"
    shutil.copy(FAKE_CLEVIS, clevis)
    clevis.chmod(0o755)
    made: list[SessionVault] = []

    def make(name: str = "secrets", **overrides) -> SessionVault:
        kwargs = {"clevis": str(clevis), **overrides}
        vault = SessionVault(base / name, **kwargs)
        real_env = vault.clevis_env

        def clevis_env() -> dict:
            env = real_env()
            if "FAKE_CLEVIS_FAIL" in os.environ:
                env["FAKE_CLEVIS_FAIL"] = os.environ["FAKE_CLEVIS_FAIL"]
            return env

        vault.clevis_env = clevis_env  # type: ignore[method-assign]
        made.append(vault)
        return vault

    yield make
    for vault in made:
        vault.wipe()
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def vault(make_vault) -> SessionVault:
    return make_vault()


def _cli_env(vault: SessionVault) -> dict:
    paths = CliPaths(
        binary=Path("/usr/local/bin/proton-drive"),
        cache_dir=vault.cli_dir,
        gnupg_home=vault.gnupg_home,
        store_dir=vault.store_dir,
        backups_dir=vault.secrets_dir.parent / "backups",
    )
    return ProtonDriveCli(paths).env()


def _pass(vault: SessionVault, *args: str, input: str | None = None, check: bool = True):
    return subprocess.run(
        ["pass", *args],
        input=input,
        capture_output=True,
        text=True,
        env=_cli_env(vault),
        check=check,
        timeout=60,
    )


def _agent_running(vault: SessionVault) -> bool:
    # gpg-connect-agent exits 0 even when no agent is running, so ask for the agent's pid instead.
    proc = subprocess.run(
        ["gpg-connect-agent", "--no-autostart", "getinfo pid", "/bye"],
        env={"PATH": "/usr/bin:/bin", "GNUPGHOME": str(vault.gnupg_home)},
        capture_output=True,
        timeout=30,
    )
    return proc.stdout.startswith(b"D ")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _passphrase(vault: SessionVault) -> str:
    """The sealed passphrase, read through the fake clevis (test-only knowledge)."""
    proc = subprocess.run(
        [vault._clevis, "decrypt"], input=vault.sealed_path.read_bytes(), capture_output=True, check=True
    )
    return proc.stdout.decode()


# --- the round trip ---


def test_create_then_unlocked_allows_pass_round_trip(vault):
    vault.create()
    assert vault.exists()
    with vault.unlocked():
        _pass(vault, "insert", "-m", "t/x", input="secret\n")
        assert _pass(vault, "show", "t/x").stdout == "secret\n"


def test_locked_store_cannot_be_read(vault):
    vault.create()
    with vault.unlocked():
        _pass(vault, "insert", "-m", "t/x", input="secret\n")
    # PASSWORD_STORE_GPG_OPTS is "--batch --pinentry-mode error": no prompt, just a failure.
    assert _pass(vault, "show", "t/x", check=False).returncode != 0


def test_agent_runs_only_while_unlocked(vault):
    vault.create()
    assert not _agent_running(vault)
    with vault.unlocked():
        assert _agent_running(vault)
    assert not _agent_running(vault)


def test_agent_is_killed_and_store_locked_when_the_body_raises(vault):
    vault.create()
    with vault.unlocked():
        _pass(vault, "insert", "-m", "t/x", input="secret\n")
    with pytest.raises(RuntimeError, match="boom"):
        with vault.unlocked():
            assert _pass(vault, "show", "t/x").stdout == "secret\n"
            raise RuntimeError("boom")
    assert not _agent_running(vault)
    assert _pass(vault, "show", "t/x", check=False).returncode != 0


def test_can_be_unlocked_again_after_a_lock(vault):
    vault.create()
    for _ in range(2):
        with vault.unlocked():
            _pass(vault, "insert", "-m", "-f", "t/x", input="secret\n")
            assert _pass(vault, "show", "t/x").stdout == "secret\n"


# --- layout and modes ---


def test_paths_are_the_documented_layout(vault):
    root = vault.secrets_dir
    assert vault.gnupg_home == root / "gnupg"
    assert vault.store_dir == root / "store"
    assert vault.cli_dir == root / "cli"
    assert vault.sealed_path == root / "passphrase.jwe"
    assert vault.keygrips_path == root / "keygrips"


def test_secrets_are_private(vault):
    vault.create()
    for p in (vault.secrets_dir, vault.gnupg_home, vault.store_dir, vault.cli_dir):
        assert stat.S_IMODE(p.stat().st_mode) == 0o700, p
    assert stat.S_IMODE(vault.sealed_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(vault.keygrips_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((vault.gnupg_home / "gpg-agent.conf").stat().st_mode) == 0o600


def test_modes_do_not_depend_on_the_process_umask(vault):
    old = os.umask(0o022)
    try:
        vault.create()
    finally:
        os.umask(old)
    assert _mode(vault.secrets_dir) == 0o700
    assert _mode(vault.sealed_path) == 0o600
    assert _mode(vault.keygrips_path) == 0o600


def test_create_writes_the_expected_files(vault):
    vault.create()
    # Presets must outlive the default 10-minute idle expiry for the length of one unlock (R-13).
    assert (vault.gnupg_home / "gpg-agent.conf").read_text() == (
        "allow-preset-passphrase\ndefault-cache-ttl 86400\nmax-cache-ttl 86400\n"
    )
    assert vault.sealed_path.read_bytes().startswith(b"FAKE:")
    grips = vault.keygrips_path.read_text().splitlines()
    # A primary key and its encryption subkey, one 40-hex keygrip per line.
    assert len(grips) >= 2
    assert all(len(g) == 40 and set(g) <= set("0123456789ABCDEF") for g in grips)
    fingerprint = (vault.store_dir / ".gpg-id").read_text().strip()
    assert len(fingerprint) in (40, 64)
    listing = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--list-secret-keys"],
        env={"PATH": "/usr/bin:/bin", "GNUPGHOME": str(vault.gnupg_home)},
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    assert f"fpr:::::::::{fingerprint}:" in listing
    assert "wow-server-sp cloud backup" in listing


def test_create_leaves_no_agent_running(vault):
    vault.create()
    assert not _agent_running(vault)


# --- exists / wipe ---


def test_exists_requires_all_three_pieces(vault):
    assert not vault.exists()
    vault.create()
    assert vault.exists()
    for piece in (vault.sealed_path, vault.keygrips_path, vault.store_dir / ".gpg-id"):
        aside = piece.with_name(piece.name + ".aside")
        piece.rename(aside)
        assert not vault.exists(), piece
        aside.rename(piece)
        assert vault.exists()


def test_wipe_removes_everything(vault):
    vault.create()
    vault.wipe()
    assert not vault.secrets_dir.exists()
    assert not vault.exists()


def test_wipe_kills_a_running_agent(vault):
    vault.create()
    with vault.unlocked():
        assert _agent_running(vault)
        vault.wipe()
        assert not vault.secrets_dir.exists()


def test_a_failing_agent_kill_is_logged(make_vault, caplog):
    vault = make_vault(gpgconf="/bin/false")
    vault.gnupg_home.mkdir(parents=True)
    with caplog.at_level("WARNING", logger="app.services.cloud_vault"):
        vault.wipe()
    assert any("Stopping the key agent exited with status 1" in r.getMessage() for r in caplog.records)
    assert not vault.secrets_dir.exists()


def test_wipe_on_a_missing_vault_is_harmless(vault):
    assert not vault.secrets_dir.exists()
    vault.wipe()
    vault.wipe()


def test_create_replaces_an_existing_vault(vault):
    vault.create()
    first = vault.keygrips_path.read_text()
    (vault.secrets_dir / "stale-file").write_text("old")
    vault.create()
    assert vault.keygrips_path.read_text() != first
    assert not (vault.secrets_dir / "stale-file").exists()


# --- failures ---


def test_unseal_failure_raises_vault_error(vault, monkeypatch):
    vault.create()
    monkeypatch.setenv("FAKE_CLEVIS_FAIL", "1")
    with pytest.raises(VaultError) as excinfo:
        with vault.unlocked():
            pass
    assert str(excinfo.value) == UNLOCK_MESSAGE
    assert not _agent_running(vault)


def test_unlocked_body_does_not_run_when_the_unseal_fails(vault, monkeypatch):
    vault.create()
    monkeypatch.setenv("FAKE_CLEVIS_FAIL", "1")
    ran = []
    with pytest.raises(VaultError):
        with vault.unlocked():
            ran.append(True)
    assert ran == []


def test_unlocked_without_a_vault_raises_vault_error(vault):
    with pytest.raises(VaultError) as excinfo:
        with vault.unlocked():
            pass
    assert str(excinfo.value) == UNLOCK_MESSAGE
    assert not vault.secrets_dir.exists()


def test_unlocked_with_missing_keygrips_raises_vault_error(vault):
    vault.create()
    vault.keygrips_path.unlink()
    with pytest.raises(VaultError):
        with vault.unlocked():
            pass
    assert not _agent_running(vault)


def test_preset_failure_raises_vault_error_and_kills_the_agent(make_vault):
    good = make_vault()
    good.create()
    broken = make_vault(preset="/bin/false")
    assert broken.secrets_dir == good.secrets_dir
    with pytest.raises(VaultError):
        with broken.unlocked():
            pass
    assert not _agent_running(good)


def test_missing_preset_helper_is_a_vault_error(make_vault):
    good = make_vault()
    good.create()
    broken = make_vault(preset="/nonexistent/gpg-preset-passphrase")
    with pytest.raises(VaultError):
        with broken.unlocked():
            pass
    assert not _agent_running(good)


def test_create_failure_raises_vault_error_and_leaves_nothing(vault, monkeypatch):
    monkeypatch.setenv("FAKE_CLEVIS_FAIL", "1")
    with pytest.raises(VaultError):
        vault.create()
    assert not vault.secrets_dir.exists()
    assert not vault.exists()


def test_create_failure_after_the_seal_also_cleans_up(make_vault):
    vault = make_vault(gpg="/nonexistent/gpg")
    with pytest.raises(VaultError):
        vault.create()
    assert not vault.secrets_dir.exists()


def test_create_failure_in_pass_init_cleans_up(make_vault):
    vault = make_vault(pass_bin="/bin/false")
    with pytest.raises(VaultError):
        vault.create()
    assert not vault.secrets_dir.exists()
    assert not vault.exists()


# --- the TPM probe ---


def test_tpm_available_true_with_a_working_clevis(vault):
    assert vault.tpm_available() is True
    # The probe is not a vault: nothing is left behind.
    assert not vault.secrets_dir.exists()


def test_tpm_available_false_when_clevis_fails(vault, monkeypatch):
    monkeypatch.setenv("FAKE_CLEVIS_FAIL", "1")
    assert vault.tpm_available() is False


def test_tpm_available_false_when_clevis_is_missing(make_vault):
    assert make_vault(clevis="/nonexistent/clevis").tpm_available() is False


def test_tpm_available_false_when_unseal_gives_back_something_else(make_vault, tmp_path):
    liar = tmp_path / "liar-clevis"
    liar.write_text("#!/bin/bash\nif [ \"$1\" = encrypt ]; then cat >/dev/null; echo token; else echo wrong; fi\n")
    liar.chmod(0o755)
    assert make_vault(clevis=str(liar)).tpm_available() is False


def test_tpm_available_false_on_timeout(make_vault, tmp_path, monkeypatch):
    slow = tmp_path / "slow-clevis"
    slow.write_text("#!/bin/bash\nexec sleep 10\n")
    slow.chmod(0o755)
    monkeypatch.setattr(cloud_vault, "CLEVIS_TIMEOUT_SECONDS", 0.3)
    assert make_vault(clevis=str(slow)).tpm_available() is False


def test_clevis_timeout_during_create_is_a_vault_error(make_vault, tmp_path, monkeypatch):
    slow = tmp_path / "slow-clevis"
    slow.write_text("#!/bin/bash\nexec sleep 10\n")
    slow.chmod(0o755)
    monkeypatch.setattr(cloud_vault, "CLEVIS_TIMEOUT_SECONDS", 0.3)
    vault = make_vault(clevis=str(slow))
    with pytest.raises(VaultError):
        vault.create()
    assert not vault.secrets_dir.exists()


def test_tpm_available_never_raises(make_vault, monkeypatch):
    vault = make_vault()

    def explode(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cloud_vault.subprocess, "run", explode)
    assert vault.tpm_available() is False


def test_clevis_env_points_at_the_tpm_resource_manager(vault):
    env = vault.clevis_env()
    assert env["TPM2TOOLS_TCTI"] == "device:/dev/tpmrm0"
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"


# --- no secrets in argv, env or files; no app environment inherited ---


@pytest.fixture
def recorded_runs(monkeypatch):
    runs: list[dict] = []
    real_run = subprocess.run

    def spy(args, **kwargs):
        runs.append({"args": [str(a) for a in args], "env": kwargs.get("env"), "timeout": kwargs.get("timeout")})
        return real_run(args, **kwargs)

    monkeypatch.setattr(cloud_vault.subprocess, "run", spy)
    return runs


def test_the_passphrase_never_reaches_argv_env_or_a_file(vault, recorded_runs, monkeypatch):
    monkeypatch.setenv("APP_ONLY_CANARY", "canary-value")
    vault.create()
    with vault.unlocked():
        pass
    vault_runs = list(recorded_runs)  # before the test's own helper runs the fake clevis
    passphrase = _passphrase(vault)
    assert len(passphrase) >= 32

    assert vault_runs, "the vault ran nothing"
    for run in vault_runs:
        assert all(passphrase not in a for a in run["args"]), run["args"]
        assert run["env"] is not None
        assert all(passphrase not in v for v in run["env"].values())
        # A fresh environment: nothing from the app's own is inherited.
        assert "APP_ONLY_CANARY" not in run["env"]
    assert passphrase not in "".join(os.environ.values())

    needle = passphrase.encode()
    for path in vault.secrets_dir.rglob("*"):
        if path.is_file():
            assert needle not in path.read_bytes(), path


def test_every_subprocess_has_a_timeout(vault, recorded_runs):
    vault.create()
    with vault.unlocked():
        pass
    vault.tpm_available()
    assert all(isinstance(run["timeout"], (int, float)) and run["timeout"] > 0 for run in recorded_runs)


def test_subprocess_environment_is_minimal_and_points_at_the_vault(vault, recorded_runs):
    vault.create()
    with vault.unlocked():
        pass
    gpg_runs = [r for r in recorded_runs if r["args"][0] in ("gpg", "pass", "gpgconf", "gpg-connect-agent")]
    assert gpg_runs
    for run in gpg_runs:
        assert run["env"]["GNUPGHOME"] == str(vault.gnupg_home)
        assert run["env"]["PASSWORD_STORE_DIR"] == str(vault.store_dir)
        assert run["env"]["PATH"] == "/usr/local/bin:/usr/bin:/bin"
        assert run["env"]["LANG"] == "C.UTF-8"
        assert run["env"].get("HOME")
    for run in recorded_runs:
        if run["args"][0].endswith("clevis"):
            assert run["env"]["TPM2TOOLS_TCTI"] == "device:/dev/tpmrm0"
            assert "GNUPGHOME" not in run["env"]


def test_commands_have_the_documented_shape(vault, recorded_runs):
    vault.create()
    with vault.unlocked():
        pass
    by_name = {}
    for run in recorded_runs:
        by_name.setdefault(Path(run["args"][0]).name, []).append(run["args"][1:])
    assert ["encrypt", "tpm2", "{}"] in by_name["clevis"]
    assert ["decrypt"] in by_name["clevis"]
    assert [
        "--batch",
        "--pinentry-mode",
        "loopback",
        "--passphrase-fd",
        "0",
        "--quick-gen-key",
        "wow-server-sp cloud backup",
        "default",
        "default",
        "never",
    ] in by_name["gpg"]
    assert ["--batch", "--with-colons", "--with-keygrip", "--list-secret-keys"] in by_name["gpg"]
    fingerprint = (vault.store_dir / ".gpg-id").read_text().strip()
    assert ["init", fingerprint] in by_name["pass"]
    presets = by_name["gpg-preset-passphrase"]
    grips = vault.keygrips_path.read_text().split()
    assert sorted(presets) == sorted([["--preset", g] for g in grips])
    assert ["--kill", "gpg-agent"] in by_name["gpgconf"]
