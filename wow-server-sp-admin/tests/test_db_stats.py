from unittest.mock import MagicMock, patch

import pytest
from mysql.connector.connection_cext import CMySQLConnection
from mysql.connector.errors import OperationalError

from app.services.db_stats import OnlineCounts, count_online


def test_connection_options_are_supported_by_pinned_connector():
    conn = CMySQLConnection()
    conn.config(
        host="ac-database",
        port=3306,
        user="root",
        password="secret",
        connection_timeout=2,
        autocommit=True,
    )


def _cursor(mock_connect, *, bots, real_rows):
    cursor = MagicMock()
    cursor.fetchone.return_value = (bots,)
    cursor.fetchall.return_value = real_rows
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cursor
    return cursor


@patch("app.services.db_stats.mysql.connector.connect")
def test_count_online_returns_split_counts(mock_connect):
    # No presence → latency fallback: 3 accounts with a pinged character.
    cursor = _cursor(mock_connect, bots=250, real_rows=[
        ("CARLOS", "Sariel", 8), ("CARLOS", "Altbot", 0),
        ("EDUARDO", "Vegivaca", 12), ("MARIA", "Nyx", 3), ("PEDRO", "Fresh", 0),
    ])

    counts = count_online(
        host="ac-database",
        port=3306,
        user="root",
        password="secret",
    )
    assert counts == OnlineCounts(real=3, bots=250)
    connection_options = mock_connect.call_args.kwargs
    assert connection_options["connection_timeout"] == 2
    assert "read_timeout" not in connection_options
    for call in cursor.execute.call_args_list:
        assert "SELECT /*+ MAX_EXECUTION_TIME(2000) */" in call.args[0]


@patch("app.services.db_stats.mysql.connector.connect")
def test_count_online_applies_the_players_page_presence_rule(mock_connect):
    # Same rule as the Players page (players.online_humans): the tracker's human
    # counts immediately (latency 0), alt-bots never do.
    _cursor(mock_connect, bots=0, real_rows=[
        ("CARLOS", "Armando", 0), ("CARLOS", "Altbot", 30), ("PEDRO", "Fresh", 0),
    ])

    counts = count_online(
        host="ac-database", port=3306, user="root", password="secret",
        presence={"CARLOS": "Armando"},
    )
    assert counts == OnlineCounts(real=1, bots=0)


@patch("app.services.db_stats.mysql.connector.connect")
def test_count_online_coerces_null_bots_to_zero(mock_connect):
    _cursor(mock_connect, bots=None, real_rows=[])
    assert count_online(host="h", port=3306, user="u", password="p") == OnlineCounts(real=0, bots=0)


def test_count_online_exits_cursor_and_closes_connection_after_query_timeout():
    class TrackingCursor:
        exited = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.exited = True

        def execute(self, _):
            raise OperationalError(msg="query timed out")

    class TrackingConnection:
        def __init__(self):
            self.cursor_instance = TrackingCursor()
            self.closed = False

        def cursor(self):
            return self.cursor_instance

        def close(self):
            self.closed = True

    conn = TrackingConnection()
    with patch("app.services.db_stats.mysql.connector.connect", return_value=conn) as connect:
        with pytest.raises(OperationalError):
            count_online(host="ac-database", port=3306, user="root", password="secret")

    assert connect.call_args.kwargs["connection_timeout"] == 2
    assert "read_timeout" not in connect.call_args.kwargs
    assert conn.cursor_instance.exited is True
    assert conn.closed is True
