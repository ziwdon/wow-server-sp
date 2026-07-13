from app.services import progression
import json
import time
from unittest.mock import MagicMock, patch

import pytest


def test_expansion_from_progression_boundaries():
    assert progression.expansion_from_state(0) == "vanilla"
    assert progression.expansion_from_state(7) == "vanilla"
    assert progression.expansion_from_state(8) == "tbc"
    assert progression.expansion_from_state(12) == "tbc"
    assert progression.expansion_from_state(13) == "wotlk"
    assert progression.expansion_from_state(18) == "wotlk"


def test_target_state_for_expansion():
    assert progression.target_state_for_expansion("vanilla") == 0
    assert progression.target_state_for_expansion("tbc") == 8
    assert progression.target_state_for_expansion("wotlk") == 13


@patch("app.services.progression.mysql.connector.connect")
def test_collect_characters_excludes_bots_and_maps_progression(mock_connect):
    cur = MagicMock()
    cur.fetchall.return_value = [
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (102, "CARLOS", "Vegivaca", 1, 6, 70, 1, 8),
        (103, "EDUARDO", "Pitocas", 3, 3, 80, 0, 13),
    ]
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    rows = progression.collect_characters(host="h", port=3306, user="u", password="p")

    assert [r.guid for r in rows] == [101, 102, 103]
    assert rows[0].expansion == "vanilla"
    assert rows[1].expansion == "tbc"
    assert rows[2].expansion == "wotlk"
    assert rows[1].online is True
    executed = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "a.username NOT LIKE 'RNDBOT%%'" in executed
    assert "a.username <> 'ahbot'" in executed
    assert "character_queststatus_rewarded" in executed


def test_validate_apply_blocks_online_character():
    row = progression.CharacterProgressionRow(1, "ACC", "Name", 1, 1, 60, True, 0, "vanilla")
    result = progression.validate_apply(row, "tbc", progression_limit=0, login_floor=0)
    assert result.ok is False
    assert result.reason == "online"


def test_validate_apply_blocks_downgrade():
    row = progression.CharacterProgressionRow(1, "ACC", "Name", 1, 1, 70, False, 8, "tbc")
    result = progression.validate_apply(row, "vanilla", progression_limit=0, login_floor=0)
    assert result.ok is False
    assert result.reason == "downgrade"


def test_validate_apply_allows_same_expansion_as_noop():
    row = progression.CharacterProgressionRow(1, "ACC", "Name", 1, 1, 70, False, 9, "tbc")
    result = progression.validate_apply(row, "tbc", progression_limit=0, login_floor=0)
    assert result.ok is True
    assert result.noop is True
    assert result.target_state == 8


def test_validate_apply_blocks_progression_limit():
    row = progression.CharacterProgressionRow(1, "ACC", "Name", 1, 1, 60, False, 0, "vanilla")
    result = progression.validate_apply(row, "wotlk", progression_limit=8, login_floor=0)
    assert result.ok is False
    assert result.reason == "progression_limit"


def test_validate_apply_blocks_below_login_floor():
    row = progression.CharacterProgressionRow(1, "ACC", "Deathy", 6, 1, 58, False, 0, "vanilla")
    result = progression.validate_apply(row, "tbc", progression_limit=0, login_floor=13)
    assert result.ok is False
    assert result.reason == "login_floor"


def test_login_floor_for_death_knight_uses_config():
    row = progression.CharacterProgressionRow(1, "ACC", "Deathy", 6, 1, 58, False, 0, "vanilla")
    cfg = progression.ProgressionConfig(progression_limit=0, starting_progression=0, tbc_races_starting=0, death_knight_starting=13)
    assert progression.login_floor_for_character(row, cfg) == 13


def test_login_floor_for_tbc_race_uses_config():
    row = progression.CharacterProgressionRow(1, "ACC", "Draenei", 2, 11, 20, False, 0, "vanilla")
    cfg = progression.ProgressionConfig(progression_limit=0, starting_progression=0, tbc_races_starting=8, death_knight_starting=13)
    assert progression.login_floor_for_character(row, cfg) == 8


def test_login_floor_uses_global_starting_progression():
    row = progression.CharacterProgressionRow(1, "ACC", "Human", 1, 1, 20, False, 0, "vanilla")
    cfg = progression.ProgressionConfig(progression_limit=0, starting_progression=8, tbc_races_starting=0, death_knight_starting=13)
    assert progression.login_floor_for_character(row, cfg) == 8


def test_config_from_resolved_keys_parses_progression_values_and_defaults():
    cfg = progression.config_from_resolved_keys([
        {"key": "IndividualProgression.ProgressionLimit", "effective_value": "13"},
        {"key": "IndividualProgression.StartingProgression", "effective_value": "8"},
        {"key": "IndividualProgression.tbcRacesStartingProgression", "effective_value": "bad"},
    ])

    assert cfg.progression_limit == 13
    assert cfg.starting_progression == 8
    assert cfg.tbc_races_starting == 0
    assert cfg.death_knight_starting == 13


@patch("app.services.progression.mysql.connector.connect")
def test_apply_progression_inserts_missing_rows_and_deletes_nothing(mock_connect, tmp_path):
    cur = MagicMock()
    # Cheap online pre-check, locking re-read, then post-commit verification.
    cur.fetchone.side_effect = [
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (8,),
    ]
    cur.fetchall.return_value = [(66001,), (66002,)]
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    result = progression.apply_progression(
        guid=101,
        target_expansion="tbc",
        config=progression.ProgressionConfig(),
        snapshots_dir=tmp_path,
        host="h",
        port=3306,
        user="u",
        password="p",
    )

    assert result.status == "applied"
    assert result.target_state == 8
    assert conn.commit.called
    executed = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "DELETE" not in executed.upper()
    assert "FOR UPDATE" in executed.upper()
    inserted_params = [call.args[1] for call in cur.execute.call_args_list if "INSERT IGNORE" in call.args[0]]
    assert inserted_params == [(101, 66003), (101, 66004), (101, 66005), (101, 66006), (101, 66007), (101, 66008)]
    audit_records = list((tmp_path / "progression-audit").glob("progression-*.json"))
    assert len(audit_records) == 1
    record = json.loads(audit_records[0].read_text())
    assert record["outcome"] == "applied"
    assert record["previous_progression"] == 0


@patch("app.services.progression.mysql.connector.connect")
def test_apply_progression_rejects_online_without_write(mock_connect, tmp_path):
    cur = MagicMock()
    cur.fetchone.return_value = (101, "CARLOS", "Sariel", 11, 4, 60, 1, 0)
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    result = progression.apply_progression(
        guid=101,
        target_expansion="tbc",
        config=progression.ProgressionConfig(),
        snapshots_dir=tmp_path,
        host="h",
        port=3306,
        user="u",
        password="p",
    )

    assert result.status == "rejected"
    assert result.reason == "online"
    assert not conn.commit.called
    executed = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "INSERT IGNORE" not in executed


@patch("app.services.progression.mysql.connector.connect")
def test_apply_progression_records_validation_rejection_best_effort(mock_connect, tmp_path):
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
    ]
    cur.fetchall.return_value = []
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    result = progression.apply_progression(
        guid=101,
        target_expansion="wotlk",
        config=progression.ProgressionConfig(progression_limit=8),
        snapshots_dir=tmp_path,
        host="h",
        port=3306,
        user="u",
        password="p",
    )

    assert result.status == "rejected"
    assert result.reason == "progression_limit"
    audit_records = list((tmp_path / "progression-audit").glob("progression-*.json"))
    assert len(audit_records) == 1
    assert json.loads(audit_records[0].read_text())["outcome"] == "validation_rejected"


@patch("app.services.progression.mysql.connector.connect")
def test_apply_progression_rolls_back_if_verification_fails(mock_connect, tmp_path):
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (7,),
    ]
    cur.fetchall.return_value = []
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    result = progression.apply_progression(
        guid=101,
        target_expansion="tbc",
        config=progression.ProgressionConfig(),
        snapshots_dir=tmp_path,
        host="h",
        port=3306,
        user="u",
        password="p",
    )

    assert result.status == "error"
    assert result.reason == "verify_failed"
    assert not conn.commit.called
    assert conn.rollback.called
    audit_records = list((tmp_path / "progression-audit").glob("progression-*.json"))
    assert len(audit_records) == 1
    assert json.loads(audit_records[0].read_text())["outcome"] == "verification_failed_rolled_back"


@patch("app.services.progression.mysql.connector.connect")
def test_apply_progression_keeps_applied_result_when_post_commit_audit_finalization_fails(mock_connect, tmp_path, caplog):
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (8,),
    ]
    cur.fetchall.return_value = []
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    with patch("app.services.progression._finalize_audit_snapshot", side_effect=OSError("audit disk full")):
        result = progression.apply_progression(
            guid=101, target_expansion="tbc", config=progression.ProgressionConfig(), snapshots_dir=tmp_path,
            host="h", port=3306, user="u", password="p",
        )

    assert result.status == "applied"
    assert conn.commit.called
    assert not conn.rollback.called
    audit_records = list((tmp_path / "progression-audit").glob("progression-*.json"))
    assert len(audit_records) == 1
    assert json.loads(audit_records[0].read_text())["outcome"] == "pending"
    assert "audit" in caplog.text.lower()


@patch("app.services.progression.mysql.connector.connect")
def test_apply_progression_keeps_applied_result_when_post_commit_audit_pruning_fails(mock_connect, tmp_path, caplog):
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (8,),
    ]
    cur.fetchall.return_value = []
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    with patch("app.services.progression._prune_progression_audit_records", side_effect=OSError("audit unavailable")):
        result = progression.apply_progression(
            guid=101, target_expansion="tbc", config=progression.ProgressionConfig(), snapshots_dir=tmp_path,
            host="h", port=3306, user="u", password="p",
        )

    assert result.status == "applied"
    assert conn.commit.called
    assert not conn.rollback.called
    audit_records = list((tmp_path / "progression-audit").glob("progression-*.json"))
    assert len(audit_records) == 1
    assert json.loads(audit_records[0].read_text())["outcome"] == "applied"
    assert "audit" in caplog.text.lower()


@patch("app.services.progression.mysql.connector.connect")
def test_apply_progression_keeps_verification_failure_when_audit_finalization_fails(mock_connect, tmp_path, caplog):
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (7,),
    ]
    cur.fetchall.return_value = []
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    with patch("app.services.progression._finalize_audit_snapshot", side_effect=OSError("audit disk full")):
        result = progression.apply_progression(
            guid=101, target_expansion="tbc", config=progression.ProgressionConfig(), snapshots_dir=tmp_path,
            host="h", port=3306, user="u", password="p",
        )

    assert result.status == "error"
    assert result.reason == "verify_failed"
    assert conn.rollback.called
    audit_records = list((tmp_path / "progression-audit").glob("progression-*.json"))
    assert len(audit_records) == 1
    assert json.loads(audit_records[0].read_text())["outcome"] == "pending"
    assert "audit" in caplog.text.lower()


@patch("app.services.progression.mysql.connector.connect")
def test_apply_progression_reraises_database_exception_when_audit_finalization_fails(mock_connect, tmp_path, caplog):
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
    ]
    cur.fetchall.return_value = []
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    with patch("app.services.progression._verify_effective_state", side_effect=RuntimeError("database lost")):
        with patch("app.services.progression._finalize_audit_snapshot", side_effect=OSError("audit disk full")):
            with pytest.raises(RuntimeError, match="database lost"):
                progression.apply_progression(
                    guid=101, target_expansion="tbc", config=progression.ProgressionConfig(), snapshots_dir=tmp_path,
                    host="h", port=3306, user="u", password="p",
                )

    audit_records = list((tmp_path / "progression-audit").glob("progression-*.json"))
    assert len(audit_records) == 1
    assert json.loads(audit_records[0].read_text())["outcome"] == "pending"
    assert "audit" in caplog.text.lower()


@patch("app.services.progression.mysql.connector.connect")
def test_apply_progression_records_exception_outcome(mock_connect, tmp_path):
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
        (101, "CARLOS", "Sariel", 11, 4, 60, 0, 0),
    ]
    cur.fetchall.return_value = []
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur

    with patch("app.services.progression._verify_effective_state", side_effect=RuntimeError("database lost")):
        with pytest.raises(RuntimeError, match="database lost"):
            progression.apply_progression(
                guid=101,
                target_expansion="tbc",
                config=progression.ProgressionConfig(),
                snapshots_dir=tmp_path,
                host="h",
                port=3306,
                user="u",
                password="p",
            )

    audit_records = list((tmp_path / "progression-audit").glob("progression-*.json"))
    assert len(audit_records) == 1
    record = json.loads(audit_records[0].read_text())
    assert record["outcome"] == "exception"
    assert record["exception_type"] == "RuntimeError"


def test_prune_progression_audit_records_keeps_100_recent_records_and_unrelated_files(tmp_path):
    audit_dir = tmp_path / "progression-audit"
    audit_dir.mkdir()
    now = int(time.time())

    for index in range(102):
        (audit_dir / f"progression-{index:03d}.json").write_text(json.dumps({
            "format_version": 1,
            "record_type": "progression_audit",
            "created_unix": now - index,
        }))
    expired = audit_dir / "progression-expired.json"
    expired.write_text(json.dumps({
        "format_version": 1,
        "record_type": "progression_audit",
        "created_unix": now - progression.PROGRESSION_AUDIT_MAX_AGE_SECONDS - 1,
    }))
    unrelated = tmp_path / "docker-compose.admin.yml.bak.1"
    unrelated.write_text("admin rollback snapshot")

    removed = progression._prune_progression_audit_records(audit_dir, now=now)

    assert removed == 3
    retained = list(audit_dir.glob("progression-*.json"))
    assert len(retained) == 100
    assert not expired.exists()
    assert unrelated.exists()


def test_prune_progression_audit_records_retains_recent_failure_outcomes(tmp_path):
    audit_dir = tmp_path / "progression-audit"
    audit_dir.mkdir()
    now = int(time.time())

    failures = {
        "progression-verification-failed.json": "verification_failed_rolled_back",
        "progression-exception.json": "exception",
    }
    for name, outcome in failures.items():
        (audit_dir / name).write_text(json.dumps({
            "format_version": 1,
            "record_type": "progression_audit",
            "outcome": outcome,
            "created_unix": now,
        }))
    (audit_dir / "progression-expired-failure.json").write_text(json.dumps({
        "format_version": 1,
        "record_type": "progression_audit",
        "outcome": "exception",
        "created_unix": now - progression.PROGRESSION_AUDIT_MAX_AGE_SECONDS - 1,
    }))

    progression._prune_progression_audit_records(audit_dir, now=now)

    assert all((audit_dir / name).exists() for name in failures)
    assert not (audit_dir / "progression-expired-failure.json").exists()


def test_lifespan_prunes_progression_audit_records(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.state import init_state

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    for name in (
        "worldserver.conf.dist",
        "playerbots.conf.dist",
        "mod_ahbot.conf.dist",
        "individualProgression.conf.dist",
    ):
        (dist_dir / name).write_text("")
    (tmp_path / "docker-compose.admin.yml").write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    (tmp_path / "docker-compose.override.yml").write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    (tmp_path / "configs").mkdir()
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    init_state(
        dist_dir=dist_dir,
        admin_yml=tmp_path / "docker-compose.admin.yml",
        override_yml=tmp_path / "docker-compose.override.yml",
        configs_dir=tmp_path / "configs",
        snapshots_dir=snapshots_dir,
    )

    with patch("app.main.progression_svc._prune_progression_audit_records") as prune:
        with TestClient(app):
            pass

    prune.assert_called_once_with(snapshots_dir / progression.PROGRESSION_AUDIT_DIRNAME)


def test_lifespan_continues_when_progression_audit_pruning_fails(tmp_path, caplog):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.state import init_state

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    for name in (
        "worldserver.conf.dist",
        "playerbots.conf.dist",
        "mod_ahbot.conf.dist",
        "individualProgression.conf.dist",
    ):
        (dist_dir / name).write_text("")
    (tmp_path / "docker-compose.admin.yml").write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    (tmp_path / "docker-compose.override.yml").write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    (tmp_path / "configs").mkdir()
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    init_state(
        dist_dir=dist_dir,
        admin_yml=tmp_path / "docker-compose.admin.yml",
        override_yml=tmp_path / "docker-compose.override.yml",
        configs_dir=tmp_path / "configs",
        snapshots_dir=snapshots_dir,
    )

    with patch("app.main.progression_svc._prune_progression_audit_records", side_effect=OSError("audit unavailable")):
        with TestClient(app):
            pass

    assert "progression audit" in caplog.text.lower()


def test_write_audit_snapshot_does_not_overwrite_same_second(tmp_path):
    row = progression.CharacterProgressionRow(101, "ACC", "Name", 1, 1, 60, False, 0, "vanilla")
    first = progression._write_audit_snapshot(
        snapshots_dir=tmp_path,
        row=row,
        target_expansion="tbc",
        target_state=8,
        existing_quests=set(),
    )
    second = progression._write_audit_snapshot(
        snapshots_dir=tmp_path,
        row=row,
        target_expansion="wotlk",
        target_state=13,
        existing_quests=set(),
    )

    assert first != second
    assert first.exists()
    assert second.exists()
