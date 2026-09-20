"""Real-player roster, online list, and character rankings for the admin Players page.

Cohort (matches stats.py): a "real player" account is
    username NOT LIKE 'RNDBOT%' AND username <> 'ahbot'
The account.username column collation is case-insensitive (utf8mb4_unicode_ci),
so the lowercase 'ahbot' literal already excludes the uppercase-stored 'AHBOT'
account — do NOT switch to LOWER(username) (redundant + non-sargable).

No background cache: real players are few, so collect_players() runs
synchronously per request (4 small queries on one connection).

"Online now" (issue #32): ``online_humans`` names the human character per
online account from the PresenceTracker snapshot (first character seen on the
account's 0 -> >=1 transition — immediate, alt-bots excluded). Accounts the
tracker has no usable memory for fall back to ``online AND latency > 0``
(``latency`` is only written on PlayerSave.Interval, so that lags ~15 min).
The dashboard "Online" card (db_stats) applies the same function so both agree.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

import mysql.connector

from app.services import wow_reference as wr

# Per-expansion level caps (mod-individual-progression / WotLK 3.3.5a).
CAP_VANILLA = 60
CAP_TBC = 70
CAP_WOTLK = 80

_REAL = "a.username NOT LIKE 'RNDBOT%%' AND a.username <> 'ahbot'"

# account -> human character name, or None when the tracker saw several
# characters appear at once and could not tell which is the human.
Presence = Mapping[str, str | None]


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
    latency: int
    last_logout: int  # unix ts of last logout; 0 = never logged in
    online_now: bool = False  # confirmed human session (set by apply_presence)


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
    faction: str
    faction_color: str
    level: int
    avg_ilvl: int | None  # None → render "—" (no equipped-gear data)


@dataclass(frozen=True)
class PvpRankRow:
    rank: int
    name: str
    class_name: str
    class_color: str
    race_name: str
    faction: str
    faction_color: str
    honor_kills: int
    honor: int


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
    top_pve: tuple[RankRow, ...]
    top_pvp: tuple[PvpRankRow, ...]


def char_row(row) -> CharRow:
    """Map a roster row: (username, name, class_id, race_id, level, online, zone_id, latency, logout_time)."""
    username, name, class_id, race_id, level, online, zone_id, latency, logout_time = row
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
        latency=int(latency or 0),
        last_logout=int(logout_time or 0),
    )


def _by_level_then_name(c: CharRow):
    # level desc, name asc (case-insensitive)
    return (-c.level, c.name.casefold())


def online_humans(
    rows: Iterable[tuple[str, str, int]], presence: Presence | None
) -> set[tuple[str, str]]:
    """(account, name) of every confirmed human session.

    ``rows`` are (account, name, latency) for characters with ``online=1`` on
    real accounts. Per account: the tracker's human wins when it is still
    online; otherwise (no memory, ambiguous, or already logged out) every
    character with ``latency > 0`` counts, as before issue #32.
    """
    by_account: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for account, name, latency in rows:
        by_account[account].append((name, latency))

    out: set[tuple[str, str]] = set()
    for account, chars in by_account.items():
        human = presence.get(account) if presence else None
        if human is not None and any(name == human for name, _ in chars):
            out.add((account, human))
            continue
        out.update((account, name) for name, latency in chars if latency > 0)
    return out


def apply_presence(chars: list[CharRow], presence: Presence | None) -> list[CharRow]:
    """Return the roster with ``online_now`` set per ``online_humans``; order preserved."""
    humans = online_humans(
        ((c.account, c.name, c.latency) for c in chars if c.online), presence
    )
    return [replace(c, online_now=(c.account, c.name) in humans) for c in chars]


def online_sorted(chars: list[CharRow]) -> tuple[CharRow, ...]:
    return tuple(sorted(
        (c for c in chars if c.online_now),
        key=_by_level_then_name,
    ))


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
        fac = wr.faction(int(race_id))
        out.append(
            RankRow(
                rank=i,
                name=str(name),
                class_name=cls,
                class_color=wr.class_color(cls),
                race_name=wr.race_name(int(race_id)),
                faction=fac,
                faction_color=wr.faction_color(fac),
                level=int(level),
                avg_ilvl=None if avg_ilvl is None else int(avg_ilvl),
            )
        )
    return tuple(out)


def pvp_rank_rows(rows) -> tuple[PvpRankRow, ...]:
    """Map pre-ordered PvP rows (name, class_id, race_id, kills, honor)."""
    out: list[PvpRankRow] = []
    for i, (name, class_id, race_id, honor_kills, honor) in enumerate(rows, start=1):
        cls = wr.class_name(int(class_id))
        fac = wr.faction(int(race_id))
        out.append(
            PvpRankRow(
                rank=i,
                name=str(name),
                class_name=cls,
                class_color=wr.class_color(cls),
                race_name=wr.race_name(int(race_id)),
                faction=fac,
                faction_color=wr.faction_color(fac),
                honor_kills=int(honor_kills or 0),
                honor=int(honor or 0),
            )
        )
    return tuple(out)


def collect_players(
    *, host: str, port: int, user: str, password: str, presence: Presence | None = None
) -> PlayersSnapshot:
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
                "SELECT a.username, c.name, c.class, c.race, c.level, c.online, c.zone, c.latency, c.logout_time "
                "FROM acore_characters.characters c "
                "JOIN acore_auth.account a ON a.id = c.account "
                f"WHERE {_REAL} "
                "ORDER BY c.level DESC, c.name ASC"
            )
            roster = apply_presence([char_row(r) for r in cur.fetchall()], presence)

            # 2. Headline aggregate (online count is derived from the roster below
            #    so the card can never disagree with the Online now list).
            cur.execute(
                "SELECT COUNT(DISTINCT a.id), "
                "SUM(c.level=60), SUM(c.level=70), SUM(c.level=80) "
                "FROM acore_auth.account a "
                "JOIN acore_characters.characters c ON c.account = a.id "
                f"WHERE {_REAL}"
            )
            h = cur.fetchone() or (0, 0, 0, 0)

            # 3. Top PvE by level, then gear (avg equipped item level), then name.
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
                "LIMIT 5"
            )
            top_pve_rows = cur.fetchall()

            # 4. Top PvP by lifetime honor kills, then current honor, then name.
            cur.execute(
                "SELECT c.name, c.class, c.race, c.totalKills, c.totalHonorPoints "
                "FROM acore_characters.characters c "
                "JOIN acore_auth.account a ON a.id = c.account "
                f"WHERE {_REAL} "
                "ORDER BY c.totalKills DESC, c.totalHonorPoints DESC, c.name ASC "
                "LIMIT 5"
            )
            top_pvp_rows = cur.fetchall()

        online_now = online_sorted(roster)
        return PlayersSnapshot(
            fetched_at=time.time(),
            total_players=int(h[0] or 0),
            online_players=len({c.account for c in online_now}),
            cap_vanilla=int(h[1] or 0),
            cap_tbc=int(h[2] or 0),
            cap_wotlk=int(h[3] or 0),
            online_now=online_now,
            all_groups=group_by_account(roster),
            top_pve=rank_rows(top_pve_rows),
            top_pvp=pvp_rank_rows(top_pvp_rows),
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
