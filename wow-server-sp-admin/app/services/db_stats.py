"""Player + bot online counts via the acore_characters schema."""

from __future__ import annotations

from dataclasses import dataclass

import mysql.connector


@dataclass(frozen=True)
class OnlineCounts:
    real: int
    bots: int


QUERY = """
SELECT
    COUNT(DISTINCT CASE WHEN a.username NOT LIKE 'RNDBOT%%' AND c.latency > 0 THEN a.id ELSE NULL END) AS real_players,
    SUM(CASE WHEN a.username LIKE 'RNDBOT%%' THEN 1 ELSE 0 END) AS bots
FROM acore_characters.characters c
JOIN acore_auth.account a ON a.id = c.account
WHERE c.online = 1
"""


def count_online(*, host: str, port: int, user: str, password: str) -> OnlineCounts:
    conn = mysql.connector.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        connection_timeout=2,
        read_timeout=2,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(QUERY)
            row = cur.fetchone() or (0, 0)
            real, bots = row
            return OnlineCounts(real=int(real or 0), bots=int(bots or 0))
    finally:
        try:
            conn.close()
        except Exception:
            pass
