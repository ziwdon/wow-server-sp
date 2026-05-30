import re
from pathlib import Path

SQL = (
    Path(__file__).resolve().parent.parent
    / "app" / "data" / "clear_rndbots.sql"
).read_text()


def test_uses_three_databases_in_order():
    uses = re.findall(r"USE\s+`(\w+)`", SQL)
    assert uses == ["acore_playerbots", "acore_characters", "acore_auth"]


def test_account_scoped_deletes_use_rndbot_or_orphan_predicate():
    # Every DELETE that references the account column must be scoped to
    # RNDBOT% or to the orphan cleanup (account NOT IN accounts).
    for line in SQL.splitlines():
        low = line.lower()
        if "delete" in low and "`account`" in low:
            assert "rndbot%" in low or "not in (select `id` from" in low, line


def test_only_rndbot_username_filter_present():
    # The only username predicate in the file is RNDBOT% (never ahbot/human).
    usernames = re.findall(r"username`?\s+LIKE\s+'([^']+)'", SQL, re.I)
    assert usernames, "expected at least one username LIKE filter"
    assert all(u.upper() == "RNDBOT%" for u in usernames), usernames


def test_no_drop_truncate_or_update():
    assert not re.search(r"\bDROP\b", SQL, re.I)
    assert not re.search(r"\bTRUNCATE\b", SQL, re.I)
    assert not re.search(r"\bUPDATE\b", SQL, re.I)


def test_no_dynamic_placeholders():
    # Static asset — no f-string / format / printf style placeholders.
    assert "{" not in SQL and "%s" not in SQL and "$(" not in SQL


def test_clears_playerbots_metadata_tables():
    assert "playerbots_random_bots" in SQL
    assert "playerbots_account_type" in SQL
