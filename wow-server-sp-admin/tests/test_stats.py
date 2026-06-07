from unittest.mock import MagicMock, patch

from app.services import stats
from app.services.stats import Bucket, StackedBucket, StackedSegment, bracket_label, brackets_from_level_rows_stacked, rows_to_buckets


def test_bracket_label_math():
    assert bracket_label(1) == "1-10"
    assert bracket_label(10) == "1-10"
    assert bracket_label(11) == "11-20"
    assert bracket_label(80) == "71-80"


def test_rows_to_buckets_class_sorted_desc():
    # (class_id, count) rows, unsorted.
    rows = [(1, 100), (8, 250), (11, 50)]
    out = rows_to_buckets(rows, label_fn=lambda i: f"C{i}")
    assert out == [Bucket("C8", 250), Bucket("C1", 100), Bucket("C11", 50)]


def test_rows_to_buckets_empty():
    assert rows_to_buckets([], label_fn=str) == []


def test_brackets_from_level_rows_groups_and_orders():
    # (level, count) rows -> 10-wide brackets, numeric order.
    rows = [(5, 3), (12, 4), (80, 9), (1, 1)]
    out = stats.brackets_from_level_rows(rows)
    assert out == [Bucket("1-10", 4), Bucket("11-20", 4), Bucket("71-80", 9)]


def test_brackets_from_level_rows_stacked_aggregates_by_type_and_online():
    # (level, account_type, online, count):
    #   type 1 online  → active   (#88c870)
    #   type 1 offline → idle     (#888070)
    #   type 2 any     → summon   (#7ab0e0)
    rows = [
        (5,  1, 1, 100),  # level 5, RNDbot, online → active
        (5,  1, 0, 300),  # level 5, RNDbot, offline → idle
        (5,  2, 0,  50),  # level 5, AddClass → summon
        (15, 1, 0, 200),  # level 15, RNDbot, offline → idle
    ]
    out = brackets_from_level_rows_stacked(rows)
    assert len(out) == 2

    b1 = next(b for b in out if b.label == "1-10")
    assert b1.total == 450
    segs = {s.color: s.count for s in b1.segments}
    assert segs["#88c870"] == 100   # active
    assert segs["#888070"] == 300   # idle
    assert segs["#7ab0e0"] == 50    # summon

    b2 = next(b for b in out if b.label == "11-20")
    assert b2.total == 200
    segs2 = {s.color: s.count for s in b2.segments}
    assert segs2["#888070"] == 200
    assert segs2["#88c870"] == 0
    assert segs2["#7ab0e0"] == 0


def test_brackets_from_level_rows_stacked_empty():
    assert brackets_from_level_rows_stacked([]) == []


def test_stacked_bucket_segments_sum_to_total():
    rows = [(10, 1, 1, 80), (10, 1, 0, 120), (10, 2, 0, 50)]
    out = brackets_from_level_rows_stacked(rows)
    assert len(out) == 1
    b = out[0]
    assert b.total == sum(s.count for s in b.segments)


@patch("app.services.stats.mysql.connector.connect")
def test_collect_stats_builds_snapshot(mock_connect):
    cur = MagicMock()
    # Order of fetches must match the order collect_stats issues queries.
    cur.fetchone.side_effect = [
        (2500, 200, 3, 1, 4, 0),  # headline: bots_total/online, players_total/online, ahbot_total/online
    ]
    cur.fetchall.side_effect = [
        # bots by level stacked: (level, account_type, online, count)
        [(5, 1, 1, 800), (5, 1, 0, 200), (40, 1, 1, 100), (40, 1, 0, 1400)],
        [(1, 1200), (8, 1300)],  # bots by class
        [(1, 1250), (2, 1250)],  # bots by race
        [(40, 3)],  # players by level
        [(1, 3)],  # players by class
        [(1, 3)],  # players by race
        [(40, 201)],  # online by level
        [(1, 201)],  # online by class
        [(1, 100), (2, 101)],   # online by race (for online_by_faction)
        [(12, 100), (1637, 101)],  # online by zone
        [(1, 1250), (2, 1250)],  # faction src rows (race_id, count) all chars
        [(1, 1200), (2, 1200)],  # faction bots
        [(1, 3), (2, 0)],  # faction players
        [(1, 1500, 170), (2, 0, 500)],  # pool breakdown (account_type, online, offline)
        [
            ("Sariel", 11, 4, 80, 251),
            ("Rndchamp", 1, 1, 80, 232),
        ],  # top PvE
        [
            ("Rndslayer", 3, 2, 99, 1200),
            ("Pitocas", 3, 3, 42, 1500),
        ],  # top PvP
    ]
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    snap = stats.collect_stats(host="h", port=3306, user="u", password="p")

    assert snap.bots_total == 2500
    assert snap.bots_online == 200
    assert snap.players_total == 3
    assert snap.ahbot_total == 4
    # Spot-check one breakdown maps ids->names and sorts desc.
    assert snap.online_by_zone[0].count >= snap.online_by_zone[-1].count
    assert {b.label for b in snap.faction_totals} <= {"Alliance", "Horde", "Unknown"}
    assert snap.fetched_at > 0
    # Stacked bot brackets: 2 brackets from the mock data
    assert len(snap.bots_by_bracket_stacked) == 2
    b1 = next(b for b in snap.bots_by_bracket_stacked if b.label == "1-10")
    assert b1.total == 1000
    segs = {s.color: s.count for s in b1.segments}
    assert segs["#88c870"] == 800   # active
    assert segs["#888070"] == 200   # idle
    assert segs["#7ab0e0"] == 0     # summon (none in mock)
    assert [r.name for r in snap.top_pve] == ["Sariel", "Rndchamp"]
    assert snap.top_pve[0].rank == 1
    assert snap.top_pve[0].avg_ilvl == 251
    assert snap.top_pve[1].class_name == "Warrior"
    assert [r.name for r in snap.top_pvp] == ["Rndslayer", "Pitocas"]
    assert snap.top_pvp[0].rank == 1
    assert snap.top_pvp[0].honor_kills == 99
    assert snap.top_pvp[1].honor == 1500

    executed_sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "pat.account_type = 1" in executed_sql
    assert "c.online = 1" in executed_sql
    assert "ORDER BY c.level DESC, avg_ilvl DESC, c.name ASC LIMIT 5" in executed_sql
    assert (
        "ORDER BY c.totalKills DESC, c.totalHonorPoints DESC, c.name ASC LIMIT 5"
        in executed_sql
    )
