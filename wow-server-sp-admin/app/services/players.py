"""Real-player roster, online list, and top-10 ranking for the admin Players page.

Cohort (matches stats.py): a "real player" account is
    username NOT LIKE 'RNDBOT%' AND username <> 'ahbot'
The account.username column collation is case-insensitive (utf8mb4_unicode_ci),
so the lowercase 'ahbot' literal already excludes the uppercase-stored 'AHBOT'
account — do NOT switch to LOWER(username) (redundant + non-sargable).

No background cache: real players are few, so collect_players() runs
synchronously per request (3 small queries on one connection).
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

import mysql.connector

from app.services import wow_reference as wr

# Per-expansion level caps (mod-individual-progression / WotLK 3.3.5a).
CAP_VANILLA = 60
CAP_TBC = 70
CAP_WOTLK = 80

_REAL = "a.username NOT LIKE 'RNDBOT%%' AND a.username <> 'ahbot'"


@dataclass(frozen=True)
class CharRow:
    account: str
    name: str
    class_name: str
    class_color: str
    race_name: str
    faction: str
    faction_color: str
    level: int
    online: bool
    zone_name: str


@dataclass(frozen=True)
class AccountGroup:
    account: str
    chars: tuple[CharRow, ...]


@dataclass(frozen=True)
class RankRow:
    rank: int
    name: str
    class_name: str
    class_color: str
    race_name: str
    level: int
    avg_ilvl: int | None  # None → render "—" (no equipped-gear data)


@dataclass(frozen=True)
class PlayersSnapshot:
    fetched_at: float
    total_players: int
    online_players: int
    cap_vanilla: int
    cap_tbc: int
    cap_wotlk: int
    online_now: tuple[CharRow, ...]
    all_groups: tuple[AccountGroup, ...]
    top10: tuple[RankRow, ...]


def char_row(row) -> CharRow:
    """Map a roster row: (username, name, class_id, race_id, level, online, zone_id)."""
    username, name, class_id, race_id, level, online, zone_id = row
    cls = wr.class_name(int(class_id))
    fac = wr.faction(int(race_id))
    return CharRow(
        account=str(username),
        name=str(name),
        class_name=cls,
        class_color=wr.class_color(cls),
        race_name=wr.race_name(int(race_id)),
        faction=fac,
        faction_color=wr.faction_color(fac),
        level=int(level),
        online=bool(online),
        zone_name=wr.zone_name(int(zone_id)),
    )


def _by_level_then_name(c: CharRow):
    # level desc, name asc (case-insensitive)
    return (-c.level, c.name.casefold())


def online_sorted(chars: list[CharRow]) -> tuple[CharRow, ...]:
    return tuple(sorted((c for c in chars if c.online), key=_by_level_then_name))


def group_by_account(chars: list[CharRow]) -> tuple[AccountGroup, ...]:
    buckets: dict[str, list[CharRow]] = defaultdict(list)
    for c in chars:
        buckets[c.account].append(c)
    groups = [
        AccountGroup(account=acct, chars=tuple(sorted(rows, key=_by_level_then_name)))
        for acct, rows in buckets.items()
    ]
    groups.sort(key=lambda g: g.account.casefold())
    return tuple(groups)


def rank_rows(rows) -> tuple[RankRow, ...]:
    """Map pre-ordered top rows (name, class_id, race_id, level, avg_ilvl) → RankRow."""
    out: list[RankRow] = []
    for i, (name, class_id, race_id, level, avg_ilvl) in enumerate(rows, start=1):
        cls = wr.class_name(int(class_id))
        out.append(
            RankRow(
                rank=i,
                name=str(name),
                class_name=cls,
                class_color=wr.class_color(cls),
                race_name=wr.race_name(int(race_id)),
                level=int(level),
                avg_ilvl=None if avg_ilvl is None else int(avg_ilvl),
            )
        )
    return tuple(out)


def collect_players(*, host: str, port: int, user: str, password: str) -> PlayersSnapshot:
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
            # 1. Roster — drives online_now + all_groups.
            cur.execute(
                "SELECT a.username, c.name, c.class, c.race, c.level, c.online, c.zone "
                "FROM acore_characters.characters c "
                "JOIN acore_auth.account a ON a.id = c.account "
                f"WHERE {_REAL} "
                "ORDER BY c.level DESC, c.name ASC"
            )
            roster = [char_row(r) for r in cur.fetchall()]

            # 2. Headline aggregate.
            cur.execute(
                "SELECT COUNT(DISTINCT a.id), "
                "COUNT(DISTINCT CASE WHEN c.online=1 THEN a.id END), "
                "SUM(c.level=60), SUM(c.level=70), SUM(c.level=80) "
                "FROM acore_auth.account a "
                "JOIN acore_characters.characters c ON c.account = a.id "
                f"WHERE {_REAL}"
            )
            h = cur.fetchone() or (0, 0, 0, 0, 0)

            # 3. Top-10 by level, then gear (avg equipped item level), then name.
            cur.execute(
                "SELECT c.name, c.class, c.race, c.level, ROUND(AVG(it.ItemLevel)) AS avg_ilvl "
                "FROM acore_characters.characters c "
                "JOIN acore_auth.account a ON a.id = c.account "
                "LEFT JOIN acore_characters.character_inventory ci "
                "  ON ci.guid = c.guid AND ci.bag = 0 AND ci.slot < 19 "
                "LEFT JOIN acore_characters.item_instance ii ON ii.guid = ci.item "
                "LEFT JOIN acore_world.item_template it ON it.entry = ii.itemEntry "
                f"WHERE {_REAL} "
                "GROUP BY c.guid, c.name, c.class, c.race, c.level "
                "ORDER BY c.level DESC, avg_ilvl DESC, c.name ASC "
                "LIMIT 10"
            )
            top_rows = cur.fetchall()

        return PlayersSnapshot(
            fetched_at=time.time(),
            total_players=int(h[0] or 0),
            online_players=int(h[1] or 0),
            cap_vanilla=int(h[2] or 0),
            cap_tbc=int(h[3] or 0),
            cap_wotlk=int(h[4] or 0),
            online_now=online_sorted(roster),
            all_groups=group_by_account(roster),
            top10=rank_rows(top_rows),
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
