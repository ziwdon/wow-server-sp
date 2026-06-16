from unittest.mock import MagicMock, patch

from app.services import players
from app.services.players import (
    AccountGroup,
    CharRow,
    PlayersSnapshot,
    PvpRankRow,
    RankRow,
    char_row,
    group_by_account,
    online_sorted,
    rank_rows,
)


def _raw(username, name, class_id, race_id, level, online, zone, latency=10):
    """A roster row as the SQL SELECT returns it."""
    return (username, name, class_id, race_id, level, online, zone, latency)


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
    b = char_row(_raw("U", "amy", 1, 1, 80, 1, 12))              # same level → name asc (amy<Bob)
    c = char_row(_raw("U", "Zed", 1, 1, 90, 0, 12))              # offline → excluded
    d = char_row(_raw("U", "Cara", 1, 1, 70, 1, 12))             # lower level → last
    e = char_row(_raw("U", "Bot", 1, 1, 60, 1, 12, latency=0))   # online altbot → excluded
    out = online_sorted([a, b, c, d, e])
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


@patch("app.services.players.mysql.connector.connect")
def test_collect_players_builds_snapshot(mock_connect):
    cur = MagicMock()
    # fetchall order matches collect_players: roster, top PvE, top PvP.
    cur.fetchall.side_effect = [
        [
            ("EDUARDO", "Vegivaca", 1, 6, 58, 0, 1637, 0),
            ("carlos", "Sariel", 11, 4, 25, 1, 1519, 8),
            ("carlos", "Tester", 1, 1, 1, 0, 12, 0),
            ("EDUARDO", "Pitocas", 3, 3, 24, 0, 1519, 0),
        ],
        [
            ("Vegivaca", 1, 6, 58, 37),
            ("Sariel", 11, 4, 25, None),
        ],
        [
            ("Pitocas", 3, 3, 42, 1500),
            ("Sariel", 11, 4, 11, 300),
        ],
    ]
    # fetchone is the (2) headline aggregate: total, online, cap60, cap70, cap80.
    cur.fetchone.return_value = (3, 1, 0, 0, 0)
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    snap = players.collect_players(host="h", port=3306, user="u", password="p")

    assert snap.total_players == 3
    assert snap.online_players == 1
    assert (snap.cap_vanilla, snap.cap_tbc, snap.cap_wotlk) == (0, 0, 0)
    # online_now: only Sariel is online
    assert [c.name for c in snap.online_now] == ["Sariel"]
    # all_groups: A→Z (carlos, EDUARDO), within-group level desc
    assert [g.account for g in snap.all_groups] == ["carlos", "EDUARDO"]
    assert [c.name for c in snap.all_groups[1].chars] == ["Vegivaca", "Pitocas"]
    # top PvE ranked; None ilvl preserved
    assert snap.top_pve[0].rank == 1 and snap.top_pve[0].name == "Vegivaca"
    assert snap.top_pve[1].avg_ilvl is None
    # top PvP ranked by total lifetime kills, then honor.
    assert snap.top_pvp[0].rank == 1 and snap.top_pvp[0].name == "Pitocas"
    assert snap.top_pvp[0].honor_kills == 42
    assert snap.top_pvp[0].honor == 1500
    assert snap.fetched_at > 0

    executed_sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "ORDER BY c.level DESC, avg_ilvl DESC, c.name ASC LIMIT 5" in executed_sql
    assert (
        "ORDER BY c.totalKills DESC, c.totalHonorPoints DESC, c.name ASC LIMIT 5"
        in executed_sql
    )


@patch("app.services.players.mysql.connector.connect")
def test_collect_players_coerces_null_headline_to_zero(mock_connect):
    cur = MagicMock()
    cur.fetchall.side_effect = [[], [], []]      # no roster, no top PvE/PvP
    cur.fetchone.return_value = (0, 0, None, None, None)  # SUM over no rows → NULL
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    snap = players.collect_players(host="h", port=3306, user="u", password="p")
    assert snap.total_players == 0
    assert (snap.cap_vanilla, snap.cap_tbc, snap.cap_wotlk) == (0, 0, 0)
    assert snap.online_now == ()
    assert snap.all_groups == ()
    assert snap.top_pve == ()
    assert snap.top_pvp == ()


from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_players_page_renders_and_nav_between_dashboard_and_stats():
    client = TestClient(app)
    resp = client.get("/players")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="players-data"' in body
    assert "refresh-players-btn" in body
    # Nav order: Dashboard < Players < Stats
    assert body.index('href="/"') < body.index('href="/players"') < body.index('href="/stats"')
    # Players link is marked active on its own page
    assert 'class="nav-link active" href="/players"' in body


from unittest.mock import patch as _patch  # noqa: E402


def _sample_snapshot():
    sariel = CharRow(
        account="CARLOS", name="Sariel", class_name="Druid", class_color="#FF7C0A",
        race_name="Night Elf", faction="Alliance", faction_color="#4080C0",
        level=25, online=True, zone_name="Stormwind City", latency=8,
    )
    return PlayersSnapshot(
        fetched_at=1716144665.0,
        total_players=2, online_players=1,
        cap_vanilla=1, cap_tbc=0, cap_wotlk=0,
        online_now=(sariel,),
        all_groups=(AccountGroup("CARLOS", (sariel,)),),
        top_pve=(RankRow(1, "Sariel", "Druid", "#FF7C0A", "Night Elf", 25, None),),
        top_pvp=(
            PvpRankRow(1, "Sariel", "Druid", "#FF7C0A", "Night Elf", 12, 450),
        ),
    )


def test_api_players_data_renders_with_snapshot():
    creds = {"host": "h", "port": 3306, "user": "u", "password": "p"}
    with _patch("app.main.players_svc.collect_players", return_value=_sample_snapshot()), \
         _patch("app.main.db_credentials", return_value=creds):
        client = TestClient(app)
        resp = client.get("/api/players/data")
    assert resp.status_code == 200
    body = resp.text
    assert "Sariel" in body
    assert "#FF7C0A" in body                       # class color applied
    assert "CARLOS" in body                        # account group header
    assert "Vanilla 60" in body                    # expansion-cap breakdown
    assert 'id="players-last-refreshed"' in body and 'hx-swap-oob="true"' in body
    assert "—" in body                             # None avg_ilvl → dash
    assert "Top characters" in body
    assert "Top PvE" in body
    assert "Top PvP" in body
    assert "Kills" in body
    assert "Honor Kills" not in body
    assert "Honor" in body
    assert '<span class="num">25</span>' in body  # PvE Level right-aligned
    assert '<span class="num">12</span>' in body  # Kills right-aligned
    assert "Top 10 characters" not in body


def test_api_players_data_db_down_shows_empty_state():
    creds = {"host": "h", "port": 3306, "user": "u", "password": "p"}
    with _patch("app.main.players_svc.collect_players", side_effect=RuntimeError("db down")), \
         _patch("app.main.db_credentials", return_value=creds):
        client = TestClient(app)
        resp = client.get("/api/players/data")
    assert resp.status_code == 200
    assert "unreachable" in resp.text.lower()


def test_existing_api_players_online_card_still_works():
    # The dashboard "Online" stat card endpoint must not collide with the page.
    from app.services.db_stats import OnlineCounts
    with _patch("app.main.db_stats.count_online", return_value=OnlineCounts(real=1, bots=2)):
        client = TestClient(app)
        resp = client.get("/api/players")
    assert resp.status_code == 200
    assert "real" in resp.text  # partials/players.html renders "N real · N bots"
