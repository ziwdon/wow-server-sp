"""Population stats via cheap GROUP BY queries over acore_characters.

One connection, ~15 read-only queries. Cohorts:
  bot    = username LIKE 'RNDBOT%'
  ahbot  = username = 'ahbot'
  player = username NOT LIKE 'RNDBOT%' AND username <> 'ahbot'

playerbots_account_type values:
  1 = RNDbot  (random roaming bots, driven by AC_AI_PLAYERBOT_MIN/MAX_RANDOM_BOTS)
  2 = AddClass (summon reserve — only spawned via .playerbots bot addclass)
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import mysql.connector

from app.services import wow_reference as wr

_RNDBOT_ACCOUNT_TYPE_RNDBOT   = 1
_RNDBOT_ACCOUNT_TYPE_ADDCLASS = 2

# All WotLK level brackets (1-10 … 71-80).
_ALL_BRACKETS: list[str] = [f"{lo}-{lo + 9}" for lo in range(1, 80, 10)]


@dataclass(frozen=True)
class Bucket:
    label: str
    count: int
    color: str = ""


_COLOR_ACTIVE = "#88c870"
_COLOR_IDLE   = "#888070"
_COLOR_SUMMON = "#7ab0e0"


@dataclass(frozen=True)
class StackedSegment:
    color: str
    count: int


@dataclass(frozen=True)
class StackedBucket:
    label: str
    total: int
    segments: tuple[StackedSegment, ...]


@dataclass(frozen=True)
class StatsSnapshot:
    fetched_at: float
    bots_total: int
    bots_online: int
    players_total: int
    players_online: int
    ahbot_total: int
    ahbot_online: int
    # Bot pool breakdown (account_type split)
    bots_active: int = 0         # type-1 RNDbot chars online
    bots_idle: int = 0           # type-1 RNDbot chars offline
    bots_summon_reserve: int = 0 # type-2 AddClass chars (all)
    bots_by_bracket: list[Bucket] = field(default_factory=list)
    bots_by_bracket_stacked: list[StackedBucket] = field(default_factory=list)
    bots_by_class: list[Bucket] = field(default_factory=list)
    bots_by_race: list[Bucket] = field(default_factory=list)
    players_by_bracket: list[Bucket] = field(default_factory=list)
    players_by_class: list[Bucket] = field(default_factory=list)
    players_by_race: list[Bucket] = field(default_factory=list)
    online_by_bracket: list[Bucket] = field(default_factory=list)
    online_by_class: list[Bucket] = field(default_factory=list)
    online_by_faction: list[Bucket] = field(default_factory=list)
    online_by_zone: list[Bucket] = field(default_factory=list)
    faction_totals: list[Bucket] = field(default_factory=list)
    faction_bots: list[Bucket] = field(default_factory=list)
    faction_players: list[Bucket] = field(default_factory=list)
    bots_pool_breakdown: list[Bucket] = field(default_factory=list)


def bracket_label(level: int) -> str:
    lo = ((level - 1) // 10) * 10 + 1
    return f"{lo}-{lo + 9}"


def rows_to_buckets(
    rows,
    *,
    label_fn: Callable[[int], str],
    color_fn: Callable[[str], str] | None = None,
) -> list[Bucket]:
    out = []
    for k, c in rows:
        label = label_fn(int(k))
        color = color_fn(label) if color_fn else ""
        out.append(Bucket(label, int(c), color))
    out.sort(key=lambda b: b.count, reverse=True)
    return out


def brackets_from_level_rows(rows, *, fill_zeros: bool = False) -> list[Bucket]:
    agg: dict[int, int] = defaultdict(int)
    for level, count in rows:
        lo = ((int(level) - 1) // 10) * 10 + 1
        agg[lo] += int(count)
    if fill_zeros:
        for lo in range(1, 80, 10):
            agg.setdefault(lo, 0)
    return [Bucket(f"{lo}-{lo + 9}", agg[lo]) for lo in sorted(agg)]


def brackets_from_level_rows_stacked(rows) -> list[StackedBucket]:
    """Aggregate (level, account_type, online, count) rows into stacked bracket buckets.

    account_type 1 (RNDbot): online=1 → active, online=0 → idle
    account_type 2 (AddClass): any → summon reserve
    """
    agg: dict[int, dict[str, int]] = defaultdict(lambda: {"active": 0, "idle": 0, "summon": 0})
    for level, account_type, online, count in rows:
        lo = ((int(level) - 1) // 10) * 10 + 1
        if account_type == _RNDBOT_ACCOUNT_TYPE_RNDBOT:
            if online:
                agg[lo]["active"] += int(count)
            else:
                agg[lo]["idle"] += int(count)
        elif account_type == _RNDBOT_ACCOUNT_TYPE_ADDCLASS:
            agg[lo]["summon"] += int(count)
    return [
        StackedBucket(
            label=f"{lo}-{lo + 9}",
            total=agg[lo]["active"] + agg[lo]["idle"] + agg[lo]["summon"],
            segments=(
                StackedSegment(_COLOR_ACTIVE, agg[lo]["active"]),
                StackedSegment(_COLOR_IDLE,   agg[lo]["idle"]),
                StackedSegment(_COLOR_SUMMON, agg[lo]["summon"]),
            ),
        )
        for lo in sorted(agg)
    ]


def faction_from_race_rows(
    rows,
    *,
    color_fn: Callable[[str], str] | None = None,
) -> list[Bucket]:
    agg: dict[str, int] = defaultdict(int)
    for race_id, count in rows:
        agg[wr.faction(int(race_id))] += int(count)
    order = {"Alliance": 0, "Horde": 1, "Unknown": 2}
    return [
        Bucket(k, agg[k], color_fn(k) if color_fn else "")
        for k in sorted(agg, key=lambda x: order.get(x, 9))
    ]


def _fill_all_classes(buckets: list[Bucket]) -> list[Bucket]:
    """Return buckets for every known class, adding zero-count entries for missing ones."""
    existing = {b.label: b for b in buckets}
    result: list[Bucket] = []
    for cls_name in wr.CLASSES.values():
        color = wr.class_color(cls_name)
        if cls_name in existing:
            b = existing[cls_name]
            result.append(Bucket(b.label, b.count, b.color or color))
        else:
            result.append(Bucket(cls_name, 0, color))
    # Non-zero first (count desc), then zeros alphabetical
    result.sort(key=lambda b: (-b.count, b.label))
    return result


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

            cur.execute(
                "SELECT c.level, pat.account_type, c.online, COUNT(*) "
                "FROM acore_characters.characters c "
                "JOIN acore_auth.account a ON a.id = c.account "
                "JOIN acore_playerbots.playerbots_account_type pat ON pat.account_id = c.account "
                f"WHERE {_BOT} "
                "GROUP BY c.level, pat.account_type, c.online"
            )
            bots_lvl_stacked = cur.fetchall()
            bots_cls  = grp(_BOT, "class")
            bots_race = grp(_BOT, "race")
            pl_lvl    = grp(_PLAYER, "level")
            pl_cls    = grp(_PLAYER, "class")
            pl_race   = grp(_PLAYER, "race")
            on_lvl    = grp("c.online=1 AND a.username <> 'ahbot'", "level")
            on_cls    = grp("c.online=1 AND a.username <> 'ahbot'", "class")
            on_race   = grp("c.online=1 AND a.username <> 'ahbot'", "race")
            on_zone   = grp("c.online=1 AND a.username <> 'ahbot'", "zone")
            fac_all   = grp("a.username <> 'ahbot'", "race")
            fac_bots  = grp(_BOT, "race")
            fac_pl    = grp(_PLAYER, "race")

            # Bot pool breakdown by account_type (RNDbot=1, AddClass=2).
            # LEFT JOIN so this doesn't fail if acore_playerbots is absent.
            cur.execute(
                "SELECT pat.account_type, "
                "SUM(CASE WHEN c.online=1 THEN 1 ELSE 0 END) AS online_n, "
                "SUM(CASE WHEN c.online=0 THEN 1 ELSE 0 END) AS offline_n "
                "FROM acore_playerbots.playerbots_account_type pat "
                "JOIN acore_auth.account a ON a.id = pat.account_id "
                "JOIN acore_characters.characters c ON c.account = pat.account_id "
                "WHERE a.username LIKE 'RNDBOT%%' "
                "GROUP BY pat.account_type"
            )
            pool_rows = cur.fetchall()

        bots_active = bots_idle = bots_summon_reserve = 0
        for acct_type, online_n, offline_n in pool_rows:
            if acct_type == _RNDBOT_ACCOUNT_TYPE_RNDBOT:
                bots_active = int(online_n or 0)
                bots_idle   = int(offline_n or 0)
            elif acct_type == _RNDBOT_ACCOUNT_TYPE_ADDCLASS:
                bots_summon_reserve = int((online_n or 0) + (offline_n or 0))

        bots_pool_breakdown = [
            Bucket("Active (online)",  bots_active,          _COLOR_ACTIVE),
            Bucket("Idle (pool slack)", bots_idle,            _COLOR_IDLE),
            Bucket("Summon reserve",   bots_summon_reserve,  _COLOR_SUMMON),
        ]

        bots_by_bracket_stacked = brackets_from_level_rows_stacked(bots_lvl_stacked)

        online_by_class_raw = rows_to_buckets(
            on_cls, label_fn=wr.class_name, color_fn=wr.class_color
        )
        online_by_class_filled = _fill_all_classes(online_by_class_raw)

        return StatsSnapshot(
            fetched_at=time.time(),
            bots_total=int(h[0] or 0),
            bots_online=int(h[1] or 0),
            players_total=int(h[2] or 0),
            players_online=int(h[3] or 0),
            ahbot_total=int(h[4] or 0),
            ahbot_online=int(h[5] or 0),
            bots_active=bots_active,
            bots_idle=bots_idle,
            bots_summon_reserve=bots_summon_reserve,
            bots_by_bracket=[Bucket(b.label, b.total) for b in bots_by_bracket_stacked],
            bots_by_bracket_stacked=bots_by_bracket_stacked,
            bots_by_class=rows_to_buckets(bots_cls, label_fn=wr.class_name, color_fn=wr.class_color),
            bots_by_race=rows_to_buckets(bots_race, label_fn=wr.race_name),
            players_by_bracket=brackets_from_level_rows(pl_lvl),
            players_by_class=rows_to_buckets(pl_cls, label_fn=wr.class_name, color_fn=wr.class_color),
            players_by_race=rows_to_buckets(pl_race, label_fn=wr.race_name),
            online_by_bracket=brackets_from_level_rows(on_lvl, fill_zeros=True),
            online_by_class=online_by_class_filled,
            online_by_faction=faction_from_race_rows(on_race, color_fn=wr.faction_color),
            online_by_zone=rows_to_buckets(on_zone, label_fn=wr.zone_name),
            faction_totals=faction_from_race_rows(fac_all, color_fn=wr.faction_color),
            faction_bots=faction_from_race_rows(fac_bots, color_fn=wr.faction_color),
            faction_players=faction_from_race_rows(fac_pl, color_fn=wr.faction_color),
            bots_pool_breakdown=bots_pool_breakdown,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
