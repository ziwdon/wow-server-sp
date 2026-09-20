"""Announce real-player logins/logouts in-game via the worldserver console.

Presence is derived from ``acore_characters.characters.online`` on real
(non-RNDBOT / non-ahbot) accounts, which AC writes synchronously at
character login and logout. The 15-min ``latency`` heuristic the Players
page uses is deliberately not reused here (see issue #32).

Per-account semantics: an account is "in" when at least one of its
characters is online. The first character to appear on the 0 -> >=1
transition is the human (alt-bots can only be summoned by an already
logged-in master), so alt-bots never cross the account boundary and stay
silent. "Offline" needs two consecutive absent polls so a quick character
swap (return to character select, pick another) announces nothing.

``acore_auth.account.online`` is not usable: alt-bot session teardown
executes ``UPDATE account SET online=0`` for the human's account.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import mysql.connector

from app.services.console import WorldserverConsole
from app.services.docker_client import WORLDSERVER, ContainerInfo, inspect_worldserver
from app.services.players import _REAL
from app.services.runner import runner as default_runner
from app.state import db_credentials

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15


# --- persisted toggle ---


@dataclass(frozen=True)
class PresenceConfig:
    announce_enabled: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PresenceConfig":
        if not isinstance(raw, dict):
            raise ValueError("presence state must be an object")
        enabled = raw.get("announce_enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("announce_enabled must be a boolean")
        return cls(announce_enabled=enabled)


class PresenceStore:
    """``presence.json`` in ADMIN_DATA_DIR; a regular file, so tmp+rename is safe."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.config_path = data_dir / "presence.json"

    def load_config(self) -> PresenceConfig:
        try:
            return PresenceConfig.from_dict(json.loads(self.config_path.read_text()))
        except FileNotFoundError:
            return PresenceConfig()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            log.warning("presence config %s unreadable; treating as disabled", self.config_path)
            return PresenceConfig()

    def save_config(self, cfg: PresenceConfig) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(cfg), indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.config_path)


def store_from_env() -> PresenceStore:
    return PresenceStore(Path(os.environ.get("ADMIN_DATA_DIR", "/admin-data")))


# --- DB probe ---


def query_online_real_characters(
    *, host: str, port: int, user: str, password: str
) -> list[tuple[str, str]]:
    """(account username, character name) for every online character on a real account."""
    conn = mysql.connector.connect(
        host=host, port=port, user=user, password=password,
        connection_timeout=2, autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.username, c.name "
                "FROM acore_characters.characters c "
                "JOIN acore_auth.account a ON a.id = c.account "
                f"WHERE c.online = 1 AND {_REAL}"
            )
            return [(str(u), str(n)) for u, n in cur.fetchall()]
    finally:
        conn.close()


# --- state machine ---


@dataclass
class _AccountState:
    human: str | None  # first character seen; None when ambiguous
    absent_polls: int = 0


def _display(account: str, human: str | None) -> str:
    """'Carlos (Armando)', or just 'Carlos' when the human character is ambiguous."""
    player = account.capitalize()
    return f"{player} ({human})" if human is not None else player


class PresenceTracker:
    """Turn successive (account, character) snapshots into announcement strings."""

    def __init__(self) -> None:
        self._accounts: dict[str, _AccountState] = {}
        self._seeded = False

    def reset(self) -> None:
        self._accounts.clear()
        self._seeded = False

    def human_character(self, account: str) -> str | None:
        state = self._accounts.get(account)
        return state.human if state is not None else None

    def observe(self, rows: Iterable[tuple[str, str]]) -> list[str]:
        present: dict[str, list[str]] = {}
        for account, name in rows:
            present.setdefault(account, []).append(name)

        if not self._seeded:
            self._accounts = {
                acct: _AccountState(human=names[0] if len(names) == 1 else None)
                for acct, names in present.items()
            }
            self._seeded = True
            return []

        messages: list[str] = []
        for account, names in present.items():
            state = self._accounts.get(account)
            if state is None:
                human = names[0] if len(names) == 1 else None
                self._accounts[account] = _AccountState(human=human)
                messages.append(f"Player {_display(account, human)} is online.")
            elif state.absent_polls:
                # Back within the debounce window: a character swap, not a logout.
                state.absent_polls = 0
                if len(names) == 1:
                    state.human = names[0]

        for account in list(self._accounts):
            if account in present:
                continue
            state = self._accounts[account]
            state.absent_polls += 1
            if state.absent_polls >= 2:
                messages.append(f"Player {_display(account, state.human)} has gone offline.")
                del self._accounts[account]
        return messages


# --- background announcer ---


class PresenceAnnouncer:
    def __init__(
        self,
        store: PresenceStore,
        *,
        query_online: Callable[..., list[tuple[str, str]]] = query_online_real_characters,
        inspect: Callable[[], ContainerInfo] = inspect_worldserver,
        console_factory: Callable[[str], Any] = WorldserverConsole,
        runner=default_runner,
        credentials: Callable[[], dict] = db_credentials,
        interval_seconds: int = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.store = store
        self.tracker = PresenceTracker()
        self._query_online = query_online
        self._inspect = inspect
        self._console_factory = console_factory
        self._runner = runner
        self._credentials = credentials
        self.interval_seconds = interval_seconds
        self._started_at: str | None = None
        self._task: asyncio.Task | None = None

    def tick(self) -> None:
        if not self.store.load_config().announce_enabled:
            self.tracker.reset()
            return
        if self._runner.current() is not None:
            return  # an admin action may own the console; skip this poll
        info = self._inspect()
        if info.status != "running":
            self.tracker.reset()
            return
        if info.started_at != self._started_at:
            # New worldserver boot: AC reset every online flag; reseed quietly.
            self.tracker.reset()
            self._started_at = info.started_at
        try:
            rows = self._query_online(**self._credentials())
        except Exception as e:  # noqa: BLE001
            log.warning("presence poll skipped: %s", e)
            return
        messages = self.tracker.observe(rows)
        if not messages:
            return
        try:
            with self._console_factory(WORLDSERVER) as console:
                for msg in messages:
                    console.send(f"announce {msg}")
        except Exception as e:  # noqa: BLE001
            log.warning("presence announce dropped (%s): %s", e, messages)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001
                log.exception("presence announcer tick failed")
            await asyncio.sleep(self.interval_seconds)
