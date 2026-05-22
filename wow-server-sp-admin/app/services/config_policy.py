"""Edit policy for config keys exposed by the settings UI."""

BLOCKED_KEYS = frozenset({"AuctionHouseBot.GUIDs"})
READ_ONLY_REASON = "installer-managed"


def read_only_reason(key: str) -> str:
    return READ_ONLY_REASON if key in BLOCKED_KEYS else ""
