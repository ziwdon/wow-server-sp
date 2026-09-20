"""Player + bot online counts via the acore_characters schema.

"Real" applies the Players page's presence rule (players.online_humans:
PresenceTracker human first, ``latency > 0`` fallback) so the dashboard
"Online" card and the Players tab always agree.
"""

from __future__ import annotations

from dataclasses import dataclass

import mysql.connector

from app.services.players import _REAL, Presence, online_humans


@dataclass(frozen=True)
class OnlineCounts:
    real: int
    bots: int


BOTS_QUERY = """
SELECT /*+ MAX_EXECUTION_TIME(2000) */ COUNT(*)
FROM acore_characters.characters c
JOIN acore_auth.account a ON a.id = c.account
WHERE c.online = 1 AND a.username LIKE 'RNDBOT%%'
"""

REAL_ONLINE_QUERY = f"""
SELECT /*+ MAX_EXECUTION_TIME(2000) */ a.username, c.name, c.latency
FROM acore_characters.characters c
JOIN acore_auth.account a ON a.id = c.account
WHERE c.online = 1 AND {_REAL}
"""


def count_online(
    *, host: str, port: int, user: str, password: str, presence: Presence | None = None
) -> OnlineCounts:
    conn = mysql.connector.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        connection_timeout=2,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(BOTS_QUERY)
            (bots,) = cur.fetchone() or (0,)
            cur.execute(REAL_ONLINE_QUERY)
            rows = [(str(u), str(n), int(lat or 0)) for u, n, lat in cur.fetchall()]
        humans = online_humans(rows, presence)
        return OnlineCounts(real=len({acct for acct, _ in humans}), bots=int(bots or 0))
    finally:
        try:
            conn.close()
        except Exception:
            pass
