"""Population stats via cheap GROUP BY queries over acore_characters.

One connection, ~13 read-only queries. Cohorts:
  bot    = username LIKE 'RNDBOT%'
  ahbot  = username = 'ahbot'
  player = username NOT LIKE 'RNDBOT%' AND username <> 'ahbot'
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import mysql.connector

from app.services import wow_reference as wr


@dataclass(frozen=True)
class Bucket:
    label: str
    count: int


@dataclass(frozen=True)
class StatsSnapshot:
    fetched_at: float
    bots_total: int
    bots_online: int
    players_total: int
    players_online: int
    ahbot_total: int
    ahbot_online: int
    bots_by_bracket: list[Bucket] = field(default_factory=list)
    bots_by_class: list[Bucket] = field(default_factory=list)
    bots_by_race: list[Bucket] = field(default_factory=list)
    players_by_bracket: list[Bucket] = field(default_factory=list)
    players_by_class: list[Bucket] = field(default_factory=list)
    players_by_race: list[Bucket] = field(default_factory=list)
    online_by_bracket: list[Bucket] = field(default_factory=list)
    online_by_class: list[Bucket] = field(default_factory=list)
    online_by_zone: list[Bucket] = field(default_factory=list)
    faction_totals: list[Bucket] = field(default_factory=list)
    faction_bots: list[Bucket] = field(default_factory=list)
    faction_players: list[Bucket] = field(default_factory=list)


def bracket_label(level: int) -> str:
    lo = ((level - 1) // 10) * 10 + 1
    return f"{lo}-{lo + 9}"


def rows_to_buckets(rows, *, label_fn: Callable[[int], str]) -> list[Bucket]:
    out = [Bucket(label_fn(int(k)), int(c)) for k, c in rows]
    out.sort(key=lambda b: b.count, reverse=True)
    return out


def brackets_from_level_rows(rows) -> list[Bucket]:
    agg: dict[int, int] = defaultdict(int)
    for level, count in rows:
        lo = ((int(level) - 1) // 10) * 10 + 1
        agg[lo] += int(count)
    return [Bucket(f"{lo}-{lo + 9}", agg[lo]) for lo in sorted(agg)]


def faction_from_race_rows(rows) -> list[Bucket]:
    agg: dict[str, int] = defaultdict(int)
    for race_id, count in rows:
        agg[wr.faction(int(race_id))] += int(count)
    order = {"Alliance": 0, "Horde": 1, "Unknown": 2}
    return [Bucket(k, agg[k]) for k in sorted(agg, key=lambda x: order.get(x, 9))]


_BOT = "a.username LIKE 'RNDBOT%%'"
_AHBOT = "a.username = 'ahbot'"
_PLAYER = "a.username NOT LIKE 'RNDBOT%%' AND a.username <> 'ahbot'"
_JOIN = (
    "FROM acore_characters.characters c "
    "JOIN acore_auth.account a ON a.id = c.account"
)


def collect_stats(*, host: str, port: int, user: str, password: str) -> StatsSnapshot:
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
            cur.execute(
                f"SELECT "
                f"SUM({_BOT}), SUM({_BOT} AND c.online=1), "
                f"SUM({_PLAYER}), SUM(({_PLAYER}) AND c.online=1), "
                f"SUM({_AHBOT}), SUM({_AHBOT} AND c.online=1) {_JOIN}"
            )
            h = cur.fetchone() or (0, 0, 0, 0, 0, 0)

            def grp(where: str, col: str):
                cur.execute(f"SELECT c.{col}, COUNT(*) {_JOIN} WHERE {where} GROUP BY c.{col}")
                return cur.fetchall()

            bots_lvl = grp(_BOT, "level")
            bots_cls = grp(_BOT, "class")
            bots_race = grp(_BOT, "race")
            pl_lvl = grp(_PLAYER, "level")
            pl_cls = grp(_PLAYER, "class")
            pl_race = grp(_PLAYER, "race")
            on_lvl = grp("c.online=1 AND a.username <> 'ahbot'", "level")
            on_cls = grp("c.online=1 AND a.username <> 'ahbot'", "class")
            on_zone = grp("c.online=1 AND a.username <> 'ahbot'", "zone")
            fac_all = grp("a.username <> 'ahbot'", "race")
            fac_bots = grp(_BOT, "race")
            fac_pl = grp(_PLAYER, "race")

        return StatsSnapshot(
            fetched_at=time.time(),
            bots_total=int(h[0] or 0),
            bots_online=int(h[1] or 0),
            players_total=int(h[2] or 0),
            players_online=int(h[3] or 0),
            ahbot_total=int(h[4] or 0),
            ahbot_online=int(h[5] or 0),
            bots_by_bracket=brackets_from_level_rows(bots_lvl),
            bots_by_class=rows_to_buckets(bots_cls, label_fn=wr.class_name),
            bots_by_race=rows_to_buckets(bots_race, label_fn=wr.race_name),
            players_by_bracket=brackets_from_level_rows(pl_lvl),
            players_by_class=rows_to_buckets(pl_cls, label_fn=wr.class_name),
            players_by_race=rows_to_buckets(pl_race, label_fn=wr.race_name),
            online_by_bracket=brackets_from_level_rows(on_lvl),
            online_by_class=rows_to_buckets(on_cls, label_fn=wr.class_name),
            online_by_zone=rows_to_buckets(on_zone, label_fn=wr.zone_name),
            faction_totals=faction_from_race_rows(fac_all),
            faction_bots=faction_from_race_rows(fac_bots),
            faction_players=faction_from_race_rows(fac_pl),
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
