"""The vault that protects the Proton Drive session on this server (spec 2, 6.2, 6.3, 6.5, 6.9).

The Proton Drive CLI keeps its session as a GPG-encrypted ``pass`` entry. The GPG
key's passphrase is random and exists in exactly one durable form: sealed to this
machine's TPM 2.0 with ``clevis encrypt tpm2 '{}'`` (``passphrase.jwe``, no PCR
policy). ``SessionVault`` owns that GPG home and password store and:

* ``create()`` makes a fresh vault: key, sealed passphrase, keygrips, ``pass init``.
* ``unlocked()`` unseals the passphrase, starts gpg-agent and pre-sets the
  passphrase in the agent's memory for every keygrip, for the duration of one
  ``with`` block (a CLI call), then kills the agent so the key is locked again.
* ``wipe()`` forgets everything (disconnect, or a vault that no longer unseals).
* ``tpm_available()`` is the functional TPM probe (seal and unseal a random value).

There is no fallback: no TPM means no vault (spec 5). The passphrase reaches
``clevis``, ``gpg`` and ``gpg-preset-passphrase`` over stdin only. It is never in
argv, in an environment, in a file or in a log, and the only copy kept after
``create()`` is the sealed one. Every subprocess gets a fresh environment (the
app's own is never inherited), an explicit timeout, and umask 077 via the child's
own ``umask`` setting (``os.umask`` would change the whole process).

The CLI is pointed at ``cli_dir``, ``gnupg_home`` and ``store_dir`` (see
``cloud_cli.CliPaths``); its GPG calls reuse the agent that ``unlocked()`` started.
The agent is started with its output sent to ``/dev/null``: a daemonised agent
that inherited a captured pipe would keep ``subprocess.run`` waiting forever.

The vault does no locking of its own; the caller holds the cloud lock (spec 6.1)
around ``create``/``unlocked``/``wipe``. gpg-agent's socket lives inside
``gnupg_home``, so the path must stay short (UNIX sockets are limited to 108
bytes); the live location ``/admin-data/cloud/secrets/gnupg`` is.
"""

from __future__ import annotations

import contextlib
import logging
import os
import pwd
import re
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)

# Looked up at call time (not bound as defaults), so a test can shorten them.
CLEVIS_TIMEOUT_SECONDS = 30
KEYGEN_TIMEOUT_SECONDS = 120
TOOL_TIMEOUT_SECONDS = 30

DEFAULT_PRESET_HELPER = "/usr/lib/gnupg/gpg-preset-passphrase"
KEY_LABEL = "wow-server-sp cloud backup"
UNLOCK_FAILED_MESSAGE = "Can't unlock the stored session — reconnect"
TPM_TCTI = "device:/dev/tpmrm0"

_SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
_PRIVATE_DIR = 0o700
_PRIVATE_FILE = 0o600
_FINGERPRINT = re.compile(r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}")  # v4 (SHA-1) or v5 (SHA-256)
_KEYGRIP = re.compile(r"[0-9A-Fa-f]{40}")


class VaultError(Exception):
    """The vault could not be created, unlocked or probed. The message is plain language."""


def _home_dir(fallback: Path) -> str:
    home = os.environ.get("HOME")
    if home:
        return home
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        # The container may run as a uid with no passwd entry; use a directory the app owns.
        return str(fallback)


def _stderr_tail(proc: subprocess.CompletedProcess) -> str:
    """The last line the tool printed, for the log. None of these tools echoes the passphrase."""
    for line in reversed((proc.stderr or b"").decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line:
            return line[:200]
    return ""


def _write_private(path: Path, data: bytes) -> None:
    """Create ``path`` mode 0600 whatever the umask is (``os.fchmod`` before any byte is written)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_FILE)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), _PRIVATE_FILE)
        handle.write(data)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, _PRIVATE_DIR)


class SessionVault:
    def __init__(
        self,
        secrets_dir: Path,
        *,
        clevis: str = "clevis",
        gpg: str = "gpg",
        gpgconf: str = "gpgconf",
        gpg_connect_agent: str = "gpg-connect-agent",
        preset: str = DEFAULT_PRESET_HELPER,
        pass_bin: str = "pass",
    ) -> None:
        self.secrets_dir = Path(secrets_dir)
        self.gnupg_home = self.secrets_dir / "gnupg"
        self.store_dir = self.secrets_dir / "store"
        self.cli_dir = self.secrets_dir / "cli"
        self.sealed_path = self.secrets_dir / "passphrase.jwe"
        self.keygrips_path = self.secrets_dir / "keygrips"
        self._clevis = clevis
        self._gpg = gpg
        self._gpgconf = gpgconf
        self._gpg_connect_agent = gpg_connect_agent
        self._preset = preset
        self._pass = pass_bin

    # --- environments ---

    def _base_env(self) -> dict[str, str]:
        return {"PATH": _SAFE_PATH, "HOME": _home_dir(self.secrets_dir), "LANG": "C.UTF-8"}

    def clevis_env(self) -> dict[str, str]:
        """Environment for clevis: fresh, with the TPM resource manager as the TPM2 tools' transport."""
        return {**self._base_env(), "TPM2TOOLS_TCTI": TPM_TCTI}

    def _gpg_env(self) -> dict[str, str]:
        """Environment for gpg, pass, gpgconf and the agent tools: this vault's home and store."""
        return {
            **self._base_env(),
            "GNUPGHOME": str(self.gnupg_home),
            "PASSWORD_STORE_DIR": str(self.store_dir),
        }

    # --- public interface ---

    def tpm_available(self) -> bool:
        """Whether this machine can seal and unseal with its TPM right now. Never raises."""
        try:
            probe = secrets.token_bytes(16)
            return self._unseal(self._seal(probe)) == probe
        except Exception as exc:  # noqa: BLE001 - any failure means "not usable"
            log.info("TPM probe failed: %s", exc)
            return False

    def exists(self) -> bool:
        """Whether a complete vault is on disk: sealed passphrase, keygrips and a ``pass`` recipient."""
        return self.sealed_path.is_file() and self.keygrips_path.is_file() and (self.store_dir / ".gpg-id").is_file()

    def create(self) -> None:
        """Replace whatever is there with a new vault. Anything that goes wrong leaves nothing behind."""
        self.wipe()
        try:
            self._create()
        except BaseException as exc:
            self.wipe()
            if isinstance(exc, OSError):
                log.warning("Could not set up the session vault: %s", exc)
                raise VaultError("Couldn't set up the secure store for the Proton Drive session.") from None
            raise

    @contextlib.contextmanager
    def unlocked(self) -> Iterator[None]:
        """Unseal the passphrase and pre-set it in gpg-agent for the length of the ``with`` block.

        On the way out, always (an exception in the block included), the agent is
        killed, which forgets the passphrase and locks the store again.
        """
        try:
            keygrips = self._read_keygrips()
            passphrase = self._unseal_passphrase()
            self._start_agent()
            self._preset_all(passphrase, keygrips)
            del passphrase
            yield
        finally:
            self._kill_agent()

    def wipe(self) -> None:
        """Kill the agent and delete the whole secrets directory. Safe on a vault that does not exist."""
        self._kill_agent()
        shutil.rmtree(self.secrets_dir, ignore_errors=True)

    # --- create ---

    def _create(self) -> None:
        for directory in (self.secrets_dir, self.gnupg_home, self.store_dir, self.cli_dir):
            _private_dir(directory)
        _write_private(self.gnupg_home / "gpg-agent.conf", b"allow-preset-passphrase\n")

        passphrase = secrets.token_urlsafe(32)
        _write_private(self.sealed_path, self._seal(passphrase.encode()))

        self._start_agent()
        stdin = (passphrase + "\n").encode()
        self._run(
            [
                self._gpg,
                "--batch",
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--quick-gen-key",
                KEY_LABEL,
                "default",
                "default",
                "never",
            ],
            what="Creating the encryption key",
            timeout=KEYGEN_TIMEOUT_SECONDS,
            stdin=stdin,
        )
        del passphrase, stdin

        listing = self._run(
            [self._gpg, "--batch", "--with-colons", "--with-keygrip", "--list-secret-keys"],
            what="Reading the encryption key",
            timeout=TOOL_TIMEOUT_SECONDS,
        )
        fingerprint, keygrips = self._parse_listing(listing.stdout.decode("utf-8", "replace"))
        _write_private(self.keygrips_path, ("\n".join(keygrips) + "\n").encode())

        self._run(
            [self._pass, "init", fingerprint],
            what="Setting up the password store",
            timeout=TOOL_TIMEOUT_SECONDS,
        )
        if not (self.store_dir / ".gpg-id").is_file():
            raise VaultError("Setting up the password store did not finish.")

        # Key generation can leave the passphrase cached; the new vault starts locked.
        self._kill_agent()
        for directory in (self.secrets_dir, self.gnupg_home, self.store_dir, self.cli_dir):
            os.chmod(directory, _PRIVATE_DIR)

    @staticmethod
    def _parse_listing(text: str) -> tuple[str, list[str]]:
        """First fingerprint and every keygrip from ``gpg --with-colons --with-keygrip --list-secret-keys``."""
        fingerprint: str | None = None
        keygrips: list[str] = []
        for line in text.splitlines():
            fields = line.split(":")
            if len(fields) < 10:
                continue
            if fields[0] == "fpr" and fingerprint is None:
                fingerprint = fields[9]
            elif fields[0] == "grp":
                keygrips.append(fields[9])
        if fingerprint is None or not _FINGERPRINT.fullmatch(fingerprint):
            raise VaultError("The new encryption key could not be read back.")
        if not keygrips or not all(_KEYGRIP.fullmatch(grip) for grip in keygrips):
            raise VaultError("The new encryption key could not be read back.")
        return fingerprint, keygrips

    # --- clevis ---

    def _seal(self, plaintext: bytes) -> bytes:
        proc = self._run(
            [self._clevis, "encrypt", "tpm2", "{}"],
            what="Sealing to the TPM",
            timeout=CLEVIS_TIMEOUT_SECONDS,
            stdin=plaintext,
            env=self.clevis_env(),
        )
        if not proc.stdout.strip():
            raise VaultError("Sealing to the TPM gave no result.")
        return proc.stdout

    def _unseal(self, sealed: bytes) -> bytes:
        proc = self._run(
            [self._clevis, "decrypt"],
            what="Unsealing from the TPM",
            timeout=CLEVIS_TIMEOUT_SECONDS,
            stdin=sealed,
            env=self.clevis_env(),
        )
        return proc.stdout

    def _unseal_passphrase(self) -> str:
        try:
            passphrase = self._unseal(self.sealed_path.read_bytes()).decode("utf-8").strip()
        except (OSError, UnicodeDecodeError, VaultError) as exc:
            log.warning("Could not unseal the stored passphrase: %s", exc)
            raise VaultError(UNLOCK_FAILED_MESSAGE) from None
        if not passphrase:
            raise VaultError(UNLOCK_FAILED_MESSAGE)
        return passphrase

    # --- gpg-agent ---

    def _read_keygrips(self) -> list[str]:
        try:
            keygrips = self.keygrips_path.read_text().split()
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("Could not read the keygrips file: %s", exc)
            raise VaultError(UNLOCK_FAILED_MESSAGE) from None
        if not keygrips or not all(_KEYGRIP.fullmatch(grip) for grip in keygrips):
            log.warning("The keygrips file is empty or malformed")
            raise VaultError(UNLOCK_FAILED_MESSAGE)
        return keygrips

    def _start_agent(self) -> None:
        """Start gpg-agent (reading ``gpg-agent.conf``) with no captured output; see the module docstring."""
        try:
            proc = subprocess.run(
                [self._gpg_connect_agent, "/bye"],
                env=self._gpg_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TOOL_TIMEOUT_SECONDS,
                umask=0o077,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise VaultError("Starting the key agent timed out.") from None
        except OSError:
            raise VaultError("The key agent could not be started (gpg-connect-agent is missing).") from None
        if proc.returncode != 0:
            raise VaultError("The key agent did not start.")

    def _kill_agent(self) -> None:
        """Stop this vault's gpg-agent, forgetting any pre-set passphrase. Errors are ignored."""
        if not self.gnupg_home.is_dir():
            return  # no home, no agent (and no reason to let gpgconf touch anything)
        try:
            subprocess.run(
                [self._gpgconf, "--kill", "gpg-agent"],
                env=self._gpg_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TOOL_TIMEOUT_SECONDS,
                umask=0o077,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - best effort; the caller is already finishing or failing
            log.warning("Could not stop the key agent: %s", exc)

    def _preset_all(self, passphrase: str, keygrips: list[str]) -> None:
        stdin = (passphrase + "\n").encode()
        for keygrip in keygrips:
            try:
                # Without -P the helper reads the passphrase from stdin, so it stays out of argv.
                self._run(
                    [self._preset, "--preset", keygrip],
                    what="Unlocking the key",
                    timeout=TOOL_TIMEOUT_SECONDS,
                    stdin=stdin,
                )
            except VaultError:
                raise VaultError(UNLOCK_FAILED_MESSAGE) from None

    # --- subprocess ---

    def _run(
        self,
        argv: list[str],
        *,
        what: str,
        timeout: float,
        stdin: bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run one tool to completion. A timeout, a missing tool or a non-zero exit is a ``VaultError``."""
        feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        try:
            proc = subprocess.run(
                argv,
                env=env if env is not None else self._gpg_env(),
                capture_output=True,
                timeout=timeout,
                umask=0o077,
                check=False,
                **feed,
            )
        except subprocess.TimeoutExpired:
            log.warning("%s timed out (%s)", what, Path(argv[0]).name)
            raise VaultError(f"{what} timed out.") from None
        except OSError as exc:
            log.warning("%s could not run %s: %s", what, argv[0], exc)
            raise VaultError(f"{what} failed: {Path(argv[0]).name} is missing or cannot be run.") from None
        if proc.returncode != 0:
            log.warning("%s failed (%s exit %s): %s", what, Path(argv[0]).name, proc.returncode, _stderr_tail(proc))
            raise VaultError(f"{what} failed.")
        return proc
