"""Send AC console commands by piping into `docker attach`'s stdin.

We use the docker CLI rather than the Python SDK's attach_socket because
the SDK multiplexes streams in a way that's awkward to write through.
The detach sequence Ctrl+P,Ctrl+Q matches what scripts/install-azerothcore.sh
Pause 2 instructs the human user to use.
"""

from __future__ import annotations

import logging
import subprocess
import time

DETACH_BYTES = b"\x10\x11"
DETACH_KEYS = "ctrl-p,ctrl-q"

log = logging.getLogger(__name__)


def format_command(cmd: str) -> bytes:
    sanitized = cmd.replace("\r", " ").replace("\n", " ").strip()
    return (sanitized + "\n").encode("utf-8")


class WorldserverConsole:
    """Open a `docker attach` subprocess, write commands, detach cleanly."""

    def __init__(self, container: str = "ac-worldserver") -> None:
        self.container = container
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "WorldserverConsole":
        self._proc = subprocess.Popen(
            ["docker", "attach", f"--detach-keys={DETACH_KEYS}", self.container],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # Give docker attach a moment to set up.
        time.sleep(0.25)
        if self._proc.poll() is not None:
            stderr = (self._proc.stderr.read() or b"").decode(errors="replace")
            raise RuntimeError(f"docker attach failed immediately: {stderr}")
        return self

    def send(self, command: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("console not open")
        log.info("worldserver_console: %s", command)
        self._proc.stdin.write(format_command(command))
        self._proc.stdin.flush()

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.write(DETACH_BYTES)
                self._proc.stdin.flush()
                self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        self._proc = None
