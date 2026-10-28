from app.services.known_errors import KNOWN_ERRORS, match_known, summarize_errors_log

EMPTY_POOL = (
    "Pool Id 1072 is empty (has no creatures, no gameobjects, no quests and no valid "
    "child pools). The pool will not be spawned."
)
GRAVEYARD = "Table `graveyard_zone` incomplete: Zone 2037 Team 0 does not have a linked graveyard"
TALK_16878 = (
    "SmartScript::ProcessAction: SMART_ACTION_TALK: EntryOrGuid 16878 SourceType 0 "
    "EventType 61 TargetType 7 using non-existent Text id 0 for talker 29264, ignored."
)


def test_each_known_line_matches_its_entry():
    assert match_known(EMPTY_POOL).key == "empty-pool"
    assert match_known(GRAVEYARD).key == "graveyard-zone"
    assert match_known(TALK_16878).key == "smart-talk-16878"


def test_smart_talk_16878_matches_any_summoned_talker():
    line = TALK_16878.replace("talker 29264", "talker 19668")
    assert match_known(line).key == "smart-talk-16878"


def test_near_misses_are_not_known():
    for line in (
        EMPTY_POOL + " Extra unexpected text.",
        EMPTY_POOL.replace("1072", "abc"),
        "prefix " + EMPTY_POOL,
        TALK_16878.replace("EntryOrGuid 16878", "EntryOrGuid 16879"),
        TALK_16878.replace("Text id 0", "Text id 1"),
        TALK_16878.replace("TargetType 7", "TargetType 1"),
        TALK_16878.replace("talker 29264", "talker abc"),
        GRAVEYARD.replace("Team 0", "Team 2"),
        "[ERROR] database connection lost",
        "",
    ):
        assert match_known(line) is None, line


def test_every_entry_explains_itself():
    keys = [entry.key for entry in KNOWN_ERRORS]
    assert len(keys) == len(set(keys))
    for entry in KNOWN_ERRORS:
        assert entry.title.strip()
        assert entry.reason.strip()


def test_missing_or_empty_file_is_clean(tmp_path):
    missing = summarize_errors_log(tmp_path / "Errors.log")
    assert missing.state == "clean"

    path = tmp_path / "Errors.log"
    path.write_text("")
    empty = summarize_errors_log(path)
    assert empty.state == "clean"
    assert empty.actionable == 0
    assert empty.known == []


def test_only_known_lines_counts_each_kind_in_list_order(tmp_path):
    path = tmp_path / "Errors.log"
    path.write_text("\n".join([TALK_16878, EMPTY_POOL, TALK_16878, TALK_16878, ""]) + "\n")

    summary = summarize_errors_log(path)

    assert summary.state == "known"
    assert summary.actionable == 0
    assert summary.known_total == 4
    order = [entry.key for entry in KNOWN_ERRORS]
    assert [entry.key for entry, _ in summary.known] == sorted(
        ["empty-pool", "smart-talk-16878"], key=order.index
    )
    assert dict((entry.key, count) for entry, count in summary.known) == {
        "empty-pool": 1,
        "smart-talk-16878": 3,
    }


def test_any_unknown_line_makes_the_log_dirty(tmp_path):
    path = tmp_path / "Errors.log"
    path.write_text(f"{EMPTY_POOL}\n[ERROR] database connection lost\n{GRAVEYARD}\n")

    summary = summarize_errors_log(path)

    assert summary.state == "dirty"
    assert summary.actionable == 1
    assert summary.known_total == 2


def test_unknown_line_far_above_the_tail_still_counts(tmp_path):
    path = tmp_path / "Errors.log"
    path.write_text("real error at the top\n" + (EMPTY_POOL + "\n") * 500)

    summary = summarize_errors_log(path)

    assert summary.state == "dirty"
    assert summary.actionable == 1
    assert summary.known_total == 500


def test_blank_lines_only_is_clean(tmp_path):
    path = tmp_path / "Errors.log"
    path.write_text("\n\n")
    assert summarize_errors_log(path).state == "clean"


def test_invalid_utf8_is_tolerated_and_actionable(tmp_path):
    path = tmp_path / "Errors.log"
    path.write_bytes(b"\xff\xfe broken bytes\n")
    summary = summarize_errors_log(path)
    assert summary.state == "dirty"
    assert summary.actionable == 1


def test_oversized_file_is_dirty_without_classifying(tmp_path):
    path = tmp_path / "Errors.log"
    path.write_text((EMPTY_POOL + "\n") * 100)

    summary = summarize_errors_log(path, max_bytes=1024)

    assert summary.too_large is True
    assert summary.state == "dirty"
    assert summary.size == path.stat().st_size
