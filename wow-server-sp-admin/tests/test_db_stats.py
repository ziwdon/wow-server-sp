from unittest.mock import MagicMock, patch

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
