"""Login/logout announcer: per-account presence tracker + announcer tick."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from app.services.docker_client import ContainerInfo
from app.services.presence import (
    PresenceAnnouncer,
    PresenceConfig,
    PresenceStore,
    PresenceTracker,
)


# --- PresenceTracker: pure state machine over (account, character) rows ---


def test_first_observation_seeds_silently():
    tracker = PresenceTracker()

    assert tracker.observe([("CARLOS", "Armando")]) == []


def test_new_account_with_one_character_announces_that_character():
    tracker = PresenceTracker()
    tracker.observe([])

    assert tracker.observe([("CARLOS", "Armando")]) == ["Player Armando is online."]


def test_new_account_with_several_characters_falls_back_to_account_name():
    tracker = PresenceTracker()
    tracker.observe([])

    msgs = tracker.observe([("CARLOS", "Armando"), ("CARLOS", "Sariel")])

    assert msgs == ["Player Carlos is online."]


def test_alt_bots_joining_an_online_account_are_silent():
    tracker = PresenceTracker()
    tracker.observe([])
    tracker.observe([("CARLOS", "Armando")])

    assert tracker.observe([("CARLOS", "Armando"), ("CARLOS", "Sariel")]) == []
    assert tracker.observe([("CARLOS", "Armando")]) == []


def test_offline_is_announced_only_after_two_consecutive_absent_polls():
    tracker = PresenceTracker()
    tracker.observe([])
    tracker.observe([("CARLOS", "Armando")])

    assert tracker.observe([]) == []
    assert tracker.observe([]) == ["Player Armando has gone offline."]
    assert tracker.observe([]) == []


def test_offline_uses_first_seen_character_not_the_alt_bots():
    tracker = PresenceTracker()
    tracker.observe([])
    tracker.observe([("CARLOS", "Armando")])
    tracker.observe([("CARLOS", "Armando"), ("CARLOS", "Sariel")])

    tracker.observe([])

    assert tracker.observe([]) == ["Player Armando has gone offline."]


def test_quick_character_swap_produces_no_announcement():
    tracker = PresenceTracker()
    tracker.observe([])
    tracker.observe([("CARLOS", "Armando")])
    tracker.observe([])  # at character select

    assert tracker.observe([("CARLOS", "Loriel")]) == []
    assert tracker.human_character("CARLOS") == "Loriel"


def test_two_accounts_are_tracked_independently():
    tracker = PresenceTracker()
    tracker.observe([])
    tracker.observe([("CARLOS", "Armando")])

    msgs = tracker.observe([("CARLOS", "Armando"), ("EDUARDO", "Vegivaca")])
    assert msgs == ["Player Vegivaca is online."]

    tracker.observe([("EDUARDO", "Vegivaca")])
    msgs = tracker.observe([("EDUARDO", "Vegivaca")])
    assert msgs == ["Player Armando has gone offline."]
    assert tracker.human_character("EDUARDO") == "Vegivaca"
    assert tracker.human_character("CARLOS") is None


def test_reset_reseeds_silently_on_next_observation():
    tracker = PresenceTracker()
    tracker.observe([])
    tracker.observe([("CARLOS", "Armando")])

    tracker.reset()

    assert tracker.observe([("EDUARDO", "Vegivaca")]) == []
    assert tracker.observe([("EDUARDO", "Vegivaca")]) == []


# --- PresenceStore: presence.json in ADMIN_DATA_DIR ---


def test_store_defaults_to_disabled(tmp_path):
    assert PresenceStore(tmp_path).load_config() == PresenceConfig(announce_enabled=False)


def test_store_round_trips(tmp_path):
    store = PresenceStore(tmp_path)

    store.save_config(PresenceConfig(announce_enabled=True))

    assert json.loads((tmp_path / "presence.json").read_text()) == {"announce_enabled": True}
    assert PresenceStore(tmp_path).load_config().announce_enabled is True


def test_store_treats_corrupt_file_as_disabled(tmp_path):
    (tmp_path / "presence.json").write_text("{not json")

    assert PresenceStore(tmp_path).load_config().announce_enabled is False


# --- PresenceAnnouncer.tick: guards + console delivery ---


RUNNING = ContainerInfo(status="running", started_at="2026-09-20T10:00:00Z", exit_code=None, image=None)
EXITED = ContainerInfo(status="exited", started_at="2026-09-20T09:00:00Z", exit_code=0, image=None)


class FakeConsole:
    sent: list[str] = []
    fail = False

    def __init__(self, container: str = "ac-worldserver") -> None:
        self.container = container

    def __enter__(self):
        if FakeConsole.fail:
            raise RuntimeError("docker attach failed immediately")
        return self

    def __exit__(self, *exc):
        return None

    def send(self, cmd: str) -> None:
        FakeConsole.sent.append(cmd)


@pytest.fixture
def console():
    FakeConsole.sent = []
    FakeConsole.fail = False
    return FakeConsole


def _announcer(tmp_path, console, *, enabled=True, rows=None, inspect=None, runner=None):
    store = PresenceStore(tmp_path)
    store.save_config(PresenceConfig(announce_enabled=enabled))
    query = Mock(side_effect=rows if rows is not None else [[]])
    runner = runner or Mock(current=Mock(return_value=None))
    ann = PresenceAnnouncer(
        store,
        query_online=query,
        inspect=inspect or Mock(return_value=RUNNING),
        console_factory=console,
        runner=runner,
        credentials=lambda: {"host": "db", "port": 3306, "user": "u", "password": "p"},
    )
    return ann, query


def test_tick_announces_transitions_over_the_console(tmp_path, console):
    ann, _ = _announcer(tmp_path, console, rows=[[], [("CARLOS", "Armando")]])

    ann.tick()
    ann.tick()

    assert console.sent == ["announce Player Armando is online."]


def test_tick_does_nothing_when_disabled(tmp_path, console):
    ann, query = _announcer(tmp_path, console, enabled=False, rows=[[], [("CARLOS", "Armando")]])

    ann.tick()
    ann.tick()

    assert query.call_count == 0
    assert console.sent == []


def test_tick_reseeds_silently_after_being_enabled(tmp_path, console):
    ann, _ = _announcer(tmp_path, console, enabled=False, rows=[[("CARLOS", "Armando")]] * 3)

    ann.tick()  # disabled: nothing observed
    ann.store.save_config(PresenceConfig(announce_enabled=True))
    ann.tick()  # first enabled tick seeds silently
    ann.tick()

    assert console.sent == []


def test_tick_skips_while_worldserver_is_not_running(tmp_path, console):
    inspect = Mock(side_effect=[RUNNING, EXITED, RUNNING, RUNNING])
    ann, query = _announcer(
        tmp_path, console,
        rows=[[("CARLOS", "Armando")], [("CARLOS", "Armando")], [("CARLOS", "Armando")]],
        inspect=inspect,
    )

    ann.tick()  # seed with Armando online
    ann.tick()  # worldserver exited: no query, state reset, no "offline"
    ann.tick()  # back: reseed silently (Armando is not re-announced)
    ann.tick()

    assert query.call_count == 3
    assert console.sent == []


def test_tick_reseeds_when_worldserver_restarted(tmp_path, console):
    rebooted = ContainerInfo(status="running", started_at="2026-09-20T11:00:00Z", exit_code=None, image=None)
    inspect = Mock(side_effect=[RUNNING, RUNNING, rebooted, rebooted])
    ann, _ = _announcer(
        tmp_path, console,
        rows=[[("CARLOS", "Armando")], [("CARLOS", "Armando")], [], []],
        inspect=inspect,
    )

    ann.tick()
    ann.tick()
    ann.tick()  # new StartedAt: everyone gone, but silent reseed
    ann.tick()

    assert console.sent == []


def test_tick_skips_while_an_admin_action_is_running(tmp_path, console):
    runner = Mock(current=Mock(side_effect=[None, object(), None]))
    ann, query = _announcer(
        tmp_path, console, rows=[[], [("CARLOS", "Armando")]], runner=runner,
    )

    ann.tick()
    ann.tick()  # action in flight: skipped entirely
    ann.tick()

    assert query.call_count == 2
    assert console.sent == ["announce Player Armando is online."]


def test_tick_swallows_console_failure_and_keeps_state(tmp_path, console):
    ann, _ = _announcer(tmp_path, console, rows=[[], [("CARLOS", "Armando")], [("CARLOS", "Armando")]])
    ann.tick()
    console.fail = True

    ann.tick()  # transition observed, delivery fails
    console.fail = False
    ann.tick()  # no re-announce: the transition was consumed

    assert console.sent == []


def test_tick_swallows_query_failure_and_keeps_state(tmp_path, console):
    rows = [[("CARLOS", "Armando")], RuntimeError("db down"), [("CARLOS", "Armando")], []]
    ann, _ = _announcer(tmp_path, console, rows=rows)

    ann.tick()
    ann.tick()  # query error: tick skipped
    ann.tick()
    ann.tick()

    assert console.sent == []
    assert ann.tracker.human_character("CARLOS") == "Armando"
