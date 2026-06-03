from unittest.mock import MagicMock, patch

from app.services import players
from app.services.players import (
    AccountGroup,
    CharRow,
    PlayersSnapshot,
    RankRow,
    char_row,
    group_by_account,
    online_sorted,
    rank_rows,
)


def _raw(username, name, class_id, race_id, level, online, zone):
    """A roster row as the SQL SELECT returns it."""
    return (username, name, class_id, race_id, level, online, zone)


def test_char_row_maps_names_colors_faction():
    # class 1 = Warrior, race 6 = Tauren (Horde), zone 1637 = Orgrimmar
    c = char_row(_raw("EDUARDO", "Vegivaca", 1, 6, 58, 0, 1637))
    assert c.account == "EDUARDO"
    assert c.name == "Vegivaca"
    assert c.class_name == "Warrior"
    assert c.class_color == "#C69B6D"
    assert c.race_name == "Tauren"
    assert c.faction == "Horde"
    assert c.faction_color == "#C03030"
    assert c.level == 58
    assert c.online is False
    assert c.zone_name == "Orgrimmar"


def test_online_sorted_filters_offline_and_orders_level_then_name():
    a = char_row(_raw("U", "Bob", 1, 1, 80, 1, 12))
    b = char_row(_raw("U", "amy", 1, 1, 80, 1, 12))   # same level → name asc (amy<Bob)
    c = char_row(_raw("U", "Zed", 1, 1, 90, 0, 12))   # offline → excluded
    d = char_row(_raw("U", "Cara", 1, 1, 70, 1, 12))  # lower level → last
    out = online_sorted([a, b, c, d])
    assert [x.name for x in out] == ["amy", "Bob", "Cara"]


def test_group_by_account_groups_az_and_orders_within_level_then_name():
    rows = [
        char_row(_raw("EDUARDO", "Vegivaca", 1, 6, 58, 0, 1637)),
        char_row(_raw("carlos", "Sariel", 11, 4, 25, 0, 1519)),
        char_row(_raw("carlos", "Tester", 1, 1, 1, 0, 12)),
        char_row(_raw("EDUARDO", "Pitocas", 3, 3, 24, 0, 1519)),
    ]
    groups = group_by_account(rows)
    # Groups ordered A→Z, case-insensitive: carlos, EDUARDO
    assert [g.account for g in groups] == ["carlos", "EDUARDO"]
    # Within carlos: level desc → Sariel(25), Tester(1)
    assert [c.name for c in groups[0].chars] == ["Sariel", "Tester"]
    # Within EDUARDO: Vegivaca(58), Pitocas(24)
    assert [c.name for c in groups[1].chars] == ["Vegivaca", "Pitocas"]


def test_rank_rows_assigns_ranks_and_preserves_none_ilvl():
    # pre-ordered top rows: (name, class_id, race_id, level, avg_ilvl)
    rows = [
        ("Vegivaca", 1, 6, 58, 37),
        ("Sariel", 11, 4, 25, None),  # no gear → None preserved (renders "—")
    ]
    ranked = rank_rows(rows)
    assert [r.rank for r in ranked] == [1, 2]
    assert ranked[0].avg_ilvl == 37
    assert ranked[1].avg_ilvl is None
    assert ranked[0].class_name == "Warrior"
    assert ranked[1].race_name == "Night Elf"
