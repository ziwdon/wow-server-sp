from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import raid_unlock as ru
from app.services.raid_unlock import RaidCharacterRow, RaidUnlockResult


# --- Task 1: whitelist + pure command builder -----------------------------

def test_raids_whitelist_has_expected_ids():
    assert {k: v.item_id for k, v in ru.RAIDS.items()} == {
        "onyxia": 16309, "molten_core": 18412, "ubrs": 12344,
        "brd": 11000, "dire_maul": 18249, "scholomance": 13704,
    }


def test_raid_choices_is_ordered_key_label_pairs():
    assert ru.raid_choices()[0] == ("onyxia", "Onyxia's Lair")
    assert ("scholomance", "Scholomance") in ru.raid_choices()


def test_build_send_command_exact_line():
    assert ru.build_send_command("Sariel", "onyxia") == (
        '.send items Sariel "Raid unlock: Onyxia\'s Lair" "Granted via admin." 16309:1'
    )


def test_build_send_command_rejects_unknown_raid():
    with pytest.raises(ValueError):
        ru.build_send_command("Sariel", "karazhan")


@pytest.mark.parametrize("bad", ["", "Sar iel", "Bob;rm", 'a"b', "N1ck"])
def test_build_send_command_rejects_invalid_name(bad):
    with pytest.raises(ValueError):
        ru.build_send_command(bad, "onyxia")


# --- Task 2: DB queries ---------------------------------------------------

@patch("app.services.raid_unlock.mysql.connector.connect")
def test_collect_characters_excludes_bots(mock_connect):
    cur = MagicMock()
    cur.fetchall.return_value = [
        (101, "CARLOS", "Sariel", 60, 0),
        (102, "CARLOS", "Vegivaca", 70, 1),
    ]
    mock_connect.return_value.cursor.return_value.__enter__.return_value = cur

    rows = ru.collect_characters(host="h", port=3306, user="u", password="p")

    assert [r.guid for r in rows] == [101, 102]
    assert rows[1].online is True and rows[1].level == 70
    executed = " ".join(c.args[0] for c in cur.execute.call_args_list)
    assert "a.username NOT LIKE 'RNDBOT%%'" in executed
    assert "a.username <> 'ahbot'" in executed
    assert "character_queststatus_rewarded" not in executed


# --- Task 3: send_raid_unlock (console + confirm poll) --------------------

class _FakeConsole:
    sent = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send(self, cmd):
        _FakeConsole.sent.append(cmd)


@patch("app.services.raid_unlock.time.sleep", lambda *_: None)
@patch("app.services.raid_unlock.WorldserverConsole", _FakeConsole)
@patch("app.services.raid_unlock.mysql.connector.connect")
def test_send_raid_unlock_confirmed(mock_connect):
    _FakeConsole.sent = []
    cur = MagicMock()
    # _resolve_name, _latest_mail_id, then _find_new_mail (found)
    cur.fetchone.side_effect = [("Sariel",), (7,), (1750000000,)]
    mock_connect.return_value.cursor.return_value.__enter__.return_value = cur

    res = ru.send_raid_unlock(guid=101, raid_key="onyxia", host="h", port=3306, user="u", password="p")

    assert res.status == "sent"
    assert res.item_name == "Drakefire Amulet"
    assert res.eta_epoch == 1750000000
    assert _FakeConsole.sent == ['.send items Sariel "Raid unlock: Onyxia\'s Lair" "Granted via admin." 16309:1']


@patch("app.services.raid_unlock.time.sleep", lambda *_: None)
@patch("app.services.raid_unlock.WorldserverConsole", _FakeConsole)
@patch("app.services.raid_unlock.mysql.connector.connect")
def test_send_raid_unlock_unconfirmed_when_no_mail(mock_connect):
    _FakeConsole.sent = []
    cur = MagicMock()
    cur.fetchone.side_effect = [("Sariel",), (7,)] + [None] * 10
    mock_connect.return_value.cursor.return_value.__enter__.return_value = cur

    res = ru.send_raid_unlock(guid=101, raid_key="onyxia", host="h", port=3306, user="u", password="p")

    assert res.status == "unconfirmed"


@patch("app.services.raid_unlock.mysql.connector.connect")
def test_send_raid_unlock_rejects_unknown_character(mock_connect):
    cur = MagicMock()
    cur.fetchone.return_value = None
    mock_connect.return_value.cursor.return_value.__enter__.return_value = cur
    with pytest.raises(ValueError):
        ru.send_raid_unlock(guid=999, raid_key="onyxia", host="h", port=3306, user="u", password="p")


@patch("app.services.raid_unlock.mysql.connector.connect")
def test_send_raid_unlock_rejects_unknown_raid_before_db(mock_connect):
    with pytest.raises(ValueError):
        ru.send_raid_unlock(guid=1, raid_key="nope", host="h", port=3306, user="u", password="p")


# --- Task 4: routes -------------------------------------------------------

def test_apply_route_happy_path(monkeypatch):
    monkeypatch.setattr("app.main.db_credentials", lambda: {"host": "h", "port": 3306, "user": "u", "password": "p"})
    monkeypatch.setattr("app.main.raid_unlock_svc.send_raid_unlock",
        lambda **kw: RaidUnlockResult("sent", "Mailed Drakefire Amulet to Sariel.", "Drakefire Amulet", 1750000000))
    r = TestClient(app).post("/api/raid-unlock/apply", json={"guid": 101, "raid_key": "onyxia"})
    assert r.status_code == 200
    assert r.json()["status"] == "sent"
    assert r.json()["item_name"] == "Drakefire Amulet"


def test_apply_route_returns_400_on_valueerror(monkeypatch):
    monkeypatch.setattr("app.main.db_credentials", lambda: {"host": "h", "port": 3306, "user": "u", "password": "p"})

    def boom(**kw):
        raise ValueError("unknown raid: nope")

    monkeypatch.setattr("app.main.raid_unlock_svc.send_raid_unlock", boom)
    r = TestClient(app).post("/api/raid-unlock/apply", json={"guid": 1, "raid_key": "nope"})
    assert r.status_code == 400


def test_apply_route_409_when_mutation_locked(monkeypatch):
    monkeypatch.setattr("app.main.runner.try_acquire_mutation", lambda: False)
    r = TestClient(app).post("/api/raid-unlock/apply", json={"guid": 1, "raid_key": "onyxia"})
    assert r.status_code == 409


def test_characters_route_renders_panel(monkeypatch):
    monkeypatch.setattr("app.main.db_credentials", lambda: {"host": "h", "port": 3306, "user": "u", "password": "p"})
    monkeypatch.setattr("app.main.raid_unlock_svc.collect_characters",
        lambda **kw: (RaidCharacterRow(101, "CARLOS", "Sariel", 60, False),))
    r = TestClient(app).get("/api/raid-unlock/characters")
    assert r.status_code == 200
    assert "Raid unlock" in r.text
    assert 'id="raid-picker"' in r.text
