"""Send AC console commands through `docker attach`'s stdin.

We use the docker CLI rather than the Python SDK's attach_socket because
the SDK multiplexes streams in a way that's awkward to write through.
The detach sequence Ctrl+P,Ctrl+Q matches what scripts/install-azerothcore.sh
Pause 2 instructs the human user to use.
"""

from __future__ import annotations

import logging
import os
import pty
import subprocess
import time
import tty

DETACH_BYTES = b"\x10\x11"
DETACH_KEYS = "ctrl-p,ctrl-q"
DETACH_SETTLE_SECONDS = 0.1

log = logging.getLogger(__name__)


def format_command(cmd: str) -> bytes:
    sanitized = cmd.replace("\r", " ").replace("\n", " ").strip()
    return (sanitized + "\n").encode("utf-8")


class WorldserverConsole:
    """Open a `docker attach` subprocess, write commands, detach cleanly."""

    def __init__(self, container: str = "ac-worldserver") -> None:
        self.container = container
        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None

    def __enter__(self) -> "WorldserverConsole":
        # ac-worldserver is TTY-enabled. Docker CLI refuses stdin attach
        # from a plain pipe in that case, so present a real PTY to it.
        master_fd, slave_fd = pty.openpty()
        tty.setraw(slave_fd)
        self._master_fd = master_fd
        try:
            self._proc = subprocess.Popen(
                ["docker", "attach", f"--detach-keys={DETACH_KEYS}", self.container],
                stdin=slave_fd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception:
            os.close(master_fd)
            self._master_fd = None
            raise
        finally:
            os.close(slave_fd)

        # Give docker attach a moment to set up.
        time.sleep(0.25)
        if self._proc.poll() is not None:
            stderr = (self._proc.stderr.read() or b"").decode(errors="replace")
            self._close_master()
            raise RuntimeError(f"docker attach failed immediately: {stderr}")
        return self

    def send(self, command: str) -> None:
        if self._proc is None or self._master_fd is None:
            raise RuntimeError("console not open")
        log.info("worldserver_console: %s", command)
        os.write(self._master_fd, format_command(command))

    def _close_master(self) -> None:
        if self._master_fd is None:
            return
        try:
            os.close(self._master_fd)
        except OSError:
            pass
        self._master_fd = None

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._proc is None:
            self._close_master()
            return
        try:
            if self._master_fd is not None:
                os.write(self._master_fd, DETACH_BYTES)
                time.sleep(DETACH_SETTLE_SECONDS)
                self._close_master()
        except (BrokenPipeError, OSError):
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        self._proc = None
        self._close_master()
