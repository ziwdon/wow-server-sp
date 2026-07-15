"""Raid unlock: mail a raid attunement/key item to a character via GM console.

Delivery uses the worldserver console command `.send items`, which mails an item
to a player by name (online or offline); AzerothCore creates the item. The item
set is a fixed whitelist — the client never supplies an item id.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import mysql.connector

from app.services.console import WorldserverConsole

log = logging.getLogger(__name__)

# WoW character names are letters only. Validate before the value reaches a
# console command line.
NAME_RE = re.compile(r"^[A-Za-z]+$")

MAIL_SUBJECT = "Raid unlock"
MAIL_BODY = "Granted via admin."

# After issuing the console command, poll briefly for AC to create the mail row
# so we can confirm delivery and report the ETA.
CONFIRM_POLL_ATTEMPTS = 6
CONFIRM_POLL_INTERVAL_SECONDS = 0.5

REAL_ACCOUNT_SQL = "a.username NOT LIKE 'RNDBOT%%' AND a.username <> 'ahbot'"


@dataclass(frozen=True)
class RaidItem:
    label: str
    item_id: int
    item_name: str


# Authoritative whitelist — the ONLY items this feature can send. Verified against
# this server's acore_world.item_template.
RAIDS: dict[str, RaidItem] = {
    "onyxia": RaidItem("Onyxia's Lair", 16309, "Drakefire Amulet"),
    "molten_core": RaidItem("Molten Core", 18412, "Core Fragment"),
    "ubrs": RaidItem("Upper Blackrock Spire", 12344, "Seal of Ascension"),
    "brd": RaidItem("Blackrock Depths", 11000, "Shadowforge Key"),
    "dire_maul": RaidItem("Dire Maul", 18249, "Crescent Key"),
    "scholomance": RaidItem("Scholomance", 13704, "Skeleton Key"),
}


def raid_choices() -> list[tuple[str, str]]:
    """Ordered (key, label) pairs for the picker."""
    return [(key, item.label) for key, item in RAIDS.items()]


@dataclass(frozen=True)
class RaidCharacterRow:
    guid: int
    account: str
    name: str
    level: int
    online: bool


@dataclass(frozen=True)
class RaidUnlockResult:
    status: str  # "sent" | "unconfirmed"
    message: str
    item_name: str
    eta_epoch: int | None = None


def build_send_command(name: str, raid_key: str) -> str:
    """Build the `.send items` console line. Raises ValueError on bad input."""
    if raid_key not in RAIDS:
        raise ValueError(f"unknown raid: {raid_key}")
    if not NAME_RE.match(name or ""):
        raise ValueError(f"invalid character name: {name!r}")
    item = RAIDS[raid_key]
    return f'.send items {name} "{MAIL_SUBJECT}: {item.label}" "{MAIL_BODY}" {item.item_id}:1'


def _connect(*, host: str, port: int, user: str, password: str):
    # autocommit=True so each poll SELECT sees the worldserver's freshly
    # committed mail row (no pinned REPEATABLE READ snapshot).
    return mysql.connector.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        connection_timeout=2,
        autocommit=True,
    )


def collect_characters(*, host: str, port: int, user: str, password: str) -> tuple[RaidCharacterRow, ...]:
    conn = _connect(host=host, port=port, user=user, password=password)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.guid, a.username, c.name, c.level, c.online "
                "FROM acore_characters.characters c "
                "JOIN acore_auth.account a ON a.id = c.account "
                f"WHERE {REAL_ACCOUNT_SQL} "
                "ORDER BY a.username ASC, c.name ASC"
            )
            return tuple(
                RaidCharacterRow(int(g), str(u), str(n), int(lv), bool(on))
                for (g, u, n, lv, on) in cur.fetchall()
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _resolve_name(cur, guid: int) -> str | None:
    cur.execute(
        "SELECT c.name FROM acore_characters.characters c "
        "JOIN acore_auth.account a ON a.id = c.account "
        f"WHERE {REAL_ACCOUNT_SQL} AND c.guid = %s",
        (guid,),
    )
    row = cur.fetchone()
    return None if row is None else str(row[0])


def _latest_mail_id(cur, guid: int) -> int:
    cur.execute(
        "SELECT COALESCE(MAX(id), 0) FROM acore_characters.mail WHERE receiver = %s",
        (guid,),
    )
    row = cur.fetchone() or (0,)
    return int(row[0] or 0)


def _find_new_mail(cur, guid: int, since_id: int, item_id: int) -> int | None:
    """deliver_time of a fresh mail to guid carrying item_id, else None."""
    cur.execute(
        "SELECT m.deliver_time FROM acore_characters.mail m "
        "JOIN acore_characters.mail_items mi ON mi.mail_id = m.id "
        "JOIN acore_characters.item_instance ii ON ii.guid = mi.item_guid "
        "WHERE m.receiver = %s AND m.id > %s AND ii.itemEntry = %s "
        "ORDER BY m.id DESC LIMIT 1",
        (guid, since_id, item_id),
    )
    row = cur.fetchone()
    return None if row is None else int(row[0])


def send_raid_unlock(*, guid: int, raid_key: str, host: str, port: int, user: str, password: str) -> RaidUnlockResult:
    if raid_key not in RAIDS:
        raise ValueError(f"unknown raid: {raid_key}")
    item = RAIDS[raid_key]
    conn = _connect(host=host, port=port, user=user, password=password)
    try:
        with conn.cursor() as cur:
            name = _resolve_name(cur, guid)
            if name is None:
                raise ValueError(f"character not found: {guid}")
            command = build_send_command(name, raid_key)  # re-validates name
            since_id = _latest_mail_id(cur, guid)

        with WorldserverConsole() as con:
            con.send(command)
        log.info(
            "raid_unlock: mailed item %d (%s) to %s (guid=%d) for raid %s",
            item.item_id, item.item_name, name, guid, raid_key,
        )

        eta: int | None = None
        with conn.cursor() as cur:
            for _ in range(CONFIRM_POLL_ATTEMPTS):
                eta = _find_new_mail(cur, guid, since_id, item.item_id)
                if eta is not None:
                    break
                time.sleep(CONFIRM_POLL_INTERVAL_SECONDS)

        if eta is None:
            return RaidUnlockResult(
                "unconfirmed",
                f"Sent {item.item_name} to {name}. Could not confirm delivery — check the character's mailbox.",
                item.item_name,
            )
        return RaidUnlockResult(
            "sent",
            f"Mailed {item.item_name} to {name}.",
            item.item_name,
            eta_epoch=eta,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
