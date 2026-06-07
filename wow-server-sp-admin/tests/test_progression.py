from app.services import progression
from unittest.mock import MagicMock, patch


def test_expansion_from_progression_boundaries():
    assert progression.expansion_from_state(0) == "vanilla"
    assert progression.expansion_from_state(7) == "vanilla"
    assert progression.expansion_from_state(8) == "tbc"
    assert progression.expansion_from_state(12) == "tbc"
    assert progression.expansion_from_state(13) == "wotlk"
    assert progression.expansion_from_state(18) == "wotlk"


def test_target_state_for_expansion():
    assert progression.target_state_for_expansion("vanilla") == 0
    assert progression.target_state_for_expansion("tbc") == 8
    assert progression.target_state_for_expansion("wotlk") == 13


@patch("app.services.progression.mysql.connector.connect")
def test_collect_characters_excludes_bots_and_maps_progression(mock_connect):
    cur = MagicMock()
    cur.fetchall.return_value = [
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (102, "CARLOS", "Vegivaca", 1, 6, 70, 1, 8),
        (103, "EDUARDO", "Pitocas", 3, 3, 80, 0, 13),
    ]
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    rows = progression.collect_characters(host="h", port=3306, user="u", password="p")

    assert [r.guid for r in rows] == [101, 102, 103]
    assert rows[0].expansion == "vanilla"
    assert rows[1].expansion == "tbc"
    assert rows[2].expansion == "wotlk"
    assert rows[1].online is True
    executed = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "a.username NOT LIKE 'RNDBOT%%'" in executed
    assert "a.username <> 'ahbot'" in executed
    assert "character_queststatus_rewarded" in executed
