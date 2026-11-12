"""Owner-facing label for the world server's Docker container state.

Display only: actions, the scheduler and the presence tracker keep reading the
raw Docker status ('missing', 'exited', ...). Since the world server stays down
after a reboot until the owner presses Start, "not running" is a normal state:
it is grey, and only an exit Docker reports as non-zero stays red.
"""

from __future__ import annotations

NOT_STARTED = frozenset({"missing", "created"})
STOPPED = frozenset({"exited", "dead"})


def status_display(status: str, exit_code: int | None) -> tuple[str, str]:
    """Return (label, tone); the tone is the `status-<tone>` CSS suffix."""
    if status in NOT_STARTED:
        # 'missing': the boot unit's `docker compose down` removed the container.
        return "NOT STARTED", "stopped"
    if status in STOPPED:
        if status == "exited" and exit_code == 0:
            return "STOPPED", "stopped"
        return "STOPPED", "failed"
    return status.upper(), status
