from unittest.mock import MagicMock, patch

import pytest
from mysql.connector.errors import OperationalError

from app.services.db_stats import OnlineCounts, count_online


@patch("app.services.db_stats.mysql.connector.connect")
def test_count_online_returns_split_counts(mock_connect):
    cursor = MagicMock()
    cursor.fetchone.return_value = (3, 250)

    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cursor

    counts = count_online(
        host="ac-database",
        port=3306,
        user="root",
        password="secret",
    )
    assert counts == OnlineCounts(real=3, bots=250)
    assert cursor.execute.called


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

    assert connect.call_args.kwargs["read_timeout"] == 2
    assert conn.cursor_instance.exited is True
    assert conn.closed is True
