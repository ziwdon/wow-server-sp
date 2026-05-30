from unittest.mock import MagicMock, patch

from app.services import stats
from app.services.stats import Bucket, bracket_label, rows_to_buckets


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


@patch("app.services.stats.mysql.connector.connect")
def test_collect_stats_builds_snapshot(mock_connect):
    cur = MagicMock()
    # Order of fetches must match the order collect_stats issues queries.
    cur.fetchone.side_effect = [
        (2500, 200, 3, 1, 4, 0),  # headline: bots_total/online, players_total/online, ahbot_total/online
    ]
    cur.fetchall.side_effect = [
        [(5, 1000), (40, 1500)],  # bots by level
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
