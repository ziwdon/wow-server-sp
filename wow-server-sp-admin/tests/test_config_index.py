import tempfile
from pathlib import Path

import pytest

from app.services.config_index import KeyEntry, build_key_index, parse_dist_file


def test_parses_three_keys_with_comments_and_defaults(tmp_path):
    fixture = Path(__file__).parent / "data" / "sample.conf.dist"
    entries = parse_dist_file(fixture)

    assert [e.key for e in entries] == ["Foo.Enable", "Bar.Count", "Baz.Name"]

    foo = entries[0]
    assert foo.default == "1"
    assert foo.inferred_type == "bool"
    assert "Whether the Foo subsystem is enabled." in foo.comment
    assert foo.source_file == fixture.name
    assert foo.line_number == 11
    assert foo.env_var == "AC_FOO_ENABLE"

    bar = entries[1]
    assert bar.default == "10"
    assert bar.inferred_type == "int"

    baz = entries[2]
    assert baz.default == ""
    assert baz.inferred_type == "string"


def test_empty_string_default_is_recognized(tmp_path):
    f = tmp_path / "x.conf.dist"
    f.write_text(
        "#\n"
        "#    K.Test\n"
        "#        Default: empty\n"
        "#\n"
        '\nK.Test = ""\n'
    )
    entries = parse_dist_file(f)
    assert entries[0].default == ""
    assert entries[0].inferred_type == "string"


def test_keys_with_floats_get_float_type(tmp_path):
    f = tmp_path / "x.conf.dist"
    f.write_text("#\n#    K\n#\n\nK = 1.5\n")
    entries = parse_dist_file(f)
    assert entries[0].inferred_type == "float"


def test_zero_and_one_are_ints_without_bool_context(tmp_path):
    f = tmp_path / "x.conf.dist"
    f.write_text("#\n#    A\n#\n\nA = 0\n#\n#    B\n#\n\nB = 1\n")
    entries = parse_dist_file(f)
    assert [e.inferred_type for e in entries] == ["int", "int"]


def test_real_keys_get_context_aware_types():
    with tempfile.TemporaryDirectory() as temp_dir:
        dist = Path(temp_dir)
        (dist / "worldserver.conf.dist").write_text(
            "#\n"
            "#    PlayerLimit\n"
            "#        Default:     1000\n"
            "\n"
            "PlayerLimit = 1000\n"
            "\n"
            "#\n"
            "#    Server.LoginInfo\n"
            "#        Description: Display core version (.server info) on login.\n"
            "#        Default:     0 - (Disabled)\n"
            "#                     1 - (Enabled)\n"
            "\n"
            "Server.LoginInfo = 0\n"
            "\n"
            "#\n"
            "#    Rate.XP.Kill\n"
            "#    Rate.XP.Quest\n"
            "#        Description: Experience rates (outside battleground)\n"
            "#        Default:     1 - (Rate.XP.Kill)\n"
            "#                     1 - (Rate.XP.Quest)\n"
            "\n"
            "Rate.XP.Kill = 1\n"
            "Rate.XP.Quest = 1\n"
        )
        (dist / "playerbots.conf.dist").write_text(
            "# Enable or disable Playerbots module\n"
            "AiPlayerbot.Enabled = 1\n"
        )
        (dist / "mod_ahbot.conf.dist").write_text(
            "#    AuctionHouseBot.GUIDs\n"
            "#        These are the character GUIDS that will be used to create auctions.\n"
            "#        It can be a single value or multiple values separated by a comma.\n"
            "#    Examples:\n"
            "#        AuctionHouseBot.GUIDs = 3,4\n"
            "\n"
            "AuctionHouseBot.GUIDs = 0\n"
        )
        (dist / "individualProgression.conf.dist").write_text("")
        index = build_key_index(dist)

    assert index["AiPlayerbot.Enabled"].inferred_type == "bool"
    assert index["Rate.XP.Kill"].inferred_type == "float"
    assert index["PlayerLimit"].inferred_type == "int"
    assert index["Server.LoginInfo"].inferred_type == "bool"
    assert index["AuctionHouseBot.GUIDs"].inferred_type == "string"


def test_grouped_comment_blocks_are_attached_to_each_documented_key(tmp_path):
    f = tmp_path / "mod_ahbot.conf.dist"
    f.write_text(
        "[worldserver]\n"
        "\n"
        "#################################################################\n"
        "# AUCTION HOUSE BOT IN-GAME COMMANDS\n"
        "#\n"
        "#    Available GM commands:\n"
        "#        .ahbot reload - Reloads AuctionHouseBot configuration\n"
        "#################################################################\n"
        "\n"
        "#################################################################\n"
        "# AUCTION HOUSE BOT SETTINGS\n"
        "#    AuctionHouseBot.DEBUG\n"
        "#        Enable/Disable Debugging output\n"
        "#    Default: false (disabled)\n"
        "#\n"
        "#    AuctionHouseBot.GUIDs\n"
        "#        These are the character GUIDS used to create auctions.\n"
        "#        It can be a single value or multiple values separated by a comma.\n"
        "#    Examples:\n"
        "#        AuctionHouseBot.GUIDs = 3,4\n"
        "#\n"
        "#    AuctionHouseBot.ItemsPerCycle\n"
        "#        How many items to post on the auction house every cycle.\n"
        "#    Default 150\n"
        "#################################################################\n"
        "\n"
        "AuctionHouseBot.DEBUG = false\n"
        "AuctionHouseBot.GUIDs = 0\n"
        "AuctionHouseBot.ItemsPerCycle = 150\n"
    )

    entries = {entry.key: entry for entry in parse_dist_file(f)}

    assert "Enable/Disable Debugging output" in entries["AuctionHouseBot.DEBUG"].comment
    assert ".ahbot reload" not in entries["AuctionHouseBot.DEBUG"].comment
    assert "character GUIDS used to create auctions" in entries["AuctionHouseBot.GUIDs"].comment
    assert "Enable/Disable Debugging output" not in entries["AuctionHouseBot.GUIDs"].comment
    assert "How many items to post" in entries["AuctionHouseBot.ItemsPerCycle"].comment


def test_build_key_index_fails_when_required_dist_files_are_missing(tmp_path):
    (tmp_path / "worldserver.conf.dist").write_text("PlayerLimit = 1000\n")

    with pytest.raises(FileNotFoundError, match="playerbots.conf.dist"):
        build_key_index(tmp_path)


def test_grouped_comment_with_dotted_prefix_keys(tmp_path):
    """Keys like AiPlayerbot.BotActiveAlone get the comment even though the comment
    block uses the short name BotActiveAlone (without the prefix)."""
    f = tmp_path / "playerbots.conf.dist"
    f.write_text(
        "####################################\n"
        "# ACTIVITY\n"
        "#\n"
        "# BotActiveAlone\n"
        "# - Controls how many bots are active.\n"
        "#\n"
        "# BotActiveAloneDurationSeconds\n"
        "# - How often the active roster rotates.\n"
        "#\n"
        "AiPlayerbot.BotActiveAlone = 10\n"
        "AiPlayerbot.BotActiveAloneDurationSeconds = 30\n"
    )
    entries = {e.key: e for e in parse_dist_file(f)}
    assert entries["AiPlayerbot.BotActiveAlone"].comment != "", (
        "BotActiveAlone should get its description via suffix match"
    )
    assert "Controls how many bots" in entries["AiPlayerbot.BotActiveAlone"].comment
    assert "active roster rotates" in entries["AiPlayerbot.BotActiveAloneDurationSeconds"].comment


def test_shared_flat_comment_reaches_all_keys(tmp_path):
    """When multiple KV lines share a single comment block (no per-key split),
    every key in that group gets the description, not just the first one."""
    f = tmp_path / "playerbots.conf.dist"
    f.write_text(
        "#\n"
        "# Force-active rules (1 = on, 0 = off)\n"
        "# InRadius  - A real player is within this many yards.\n"
        "# InZone    - A real player is in the same zone.\n"
        "# InGuild   - The bot is in a guild with a real player.\n"
        "#\n"
        "Foo.ForceWhenInRadius = 150\n"
        "Foo.ForceWhenInZone = 1\n"
        "Foo.ForceWhenInGuild = 1\n"
    )
    entries = {e.key: e for e in parse_dist_file(f)}
    assert entries["Foo.ForceWhenInRadius"].comment != ""
    assert entries["Foo.ForceWhenInZone"].comment != "", (
        "Second key in shared block should also get the description"
    )
    assert entries["Foo.ForceWhenInGuild"].comment != "", (
        "Third key in shared block should also get the description"
    )
    assert (
        entries["Foo.ForceWhenInZone"].comment
        == entries["Foo.ForceWhenInRadius"].comment
    ), "All keys in a shared flat block should have the same description"
