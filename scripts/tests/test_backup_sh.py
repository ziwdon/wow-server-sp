import json
import os
import stat
import subprocess
import tarfile
import time
from pathlib import Path

SCRIPTS_DIR = Path("/src") if Path("/src/backup.sh").is_file() else Path(__file__).resolve().parents[1]
BACKUP_SH = SCRIPTS_DIR / "backup.sh"
DBS = ("acore_auth", "acore_characters", "acore_world", "acore_playerbots")


def _v2_dump() -> str:
    sections = "\n".join(
        f"-- Current Database: `{db}`\nCREATE DATABASE `{db}`;\nUSE `{db}`;"
        for db in DBS
    )
    return f"-- MySQL dump 10.13\n{sections}\n-- Dump completed on 2026-07-14 12:00:00\n"


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _stack(tmp_path: Path) -> Path:
    """A fake STACK_DIR with .env, configs/, and a fake git repo."""
    stack = tmp_path / "stack"
    (stack / "configs" / "mysql").mkdir(parents=True)
    (stack / "configs" / "modules").mkdir(parents=True)
    (stack / "modules" / "mod-playerbots" / ".git").mkdir(parents=True)
    (stack / ".git").mkdir()
    (stack / ".env").write_text(
        'DOCKER_DB_ROOT_PASSWORD=secret\nDOCKER_IMAGE_TAG=playerbot-local\n'
    )
    (stack / "docker-compose.override.yml").write_text("services: {}\n")
    (stack / "docker-compose.admin.yml").write_text("services: {}\n")
    (stack / "configs" / "modules" / "mod_ahbot.conf").write_text(
        "AuctionHouseBot.GUIDs = 100,101\n"
    )
    (stack / "configs" / "mysql" / "custom.cnf").write_text("[mysqld]\n")
    return stack


def _stubs(tmp_path: Path) -> Path:
    """A bin dir stubbing `docker` (mysqldump emits fake SQL) and `git`."""
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_stub(bind / "docker", (
        '#!/bin/bash\n'
        '[ -n "${DOCKER_CALLS_LOG:-}" ] && echo "$@" >> "$DOCKER_CALLS_LOG"\n'
        '[ -n "${DOCKER_ARGS_NUL:-}" ] && { printf "%s\\0" "$@"; printf "\\1"; } >> "$DOCKER_ARGS_NUL"\n'
        'case "$1" in\n'
        '  inspect) [ "${FAIL_DOCKER_INSPECT:-}" = 1 ] && exit 42; exit 0 ;;\n'
        '  exec)\n'
        '    shift\n'
        '    if printf "%s " "$@" | grep -q mysqldump; then\n'
        '      if [ "${BLOCK_MYSQLDUMP:-}" = 1 ]; then\n'
        '        touch "${BACKUP_STARTED:?}"\n'
        '        while [ ! -f "${BACKUP_RELEASE:?}" ]; do sleep 0.02; done\n'
        '      fi\n'
        '      [ "${FAIL_MYSQLDUMP:-}" = 1 ] && { echo "-- partial dump --"; exit 42; }\n'
        '      if [ "${MALFORMED_SUCCESS:-0}" = 1 ]; then\n'
        '        printf '"'"'%s\\n'"'"' '"'"'-- exit-zero malformed dump --'"'"'\n'
        '      else\n'
        '        printf '"'"'%s'"'"' "$CANONICAL_DUMP"\n'
        '      fi\n'
        '      exit 0\n'
        '    fi\n'
        '    # `mysql -e "USE db"` existence probe — succeed for all four.\n'
        '    [ -n "${FAIL_DB_NAME:-}" ] && printf "%s " "$@" | grep -q "USE ${FAIL_DB_NAME}" && exit 42\n'
        '    [ "${FAIL_DB_PROBE:-}" = 1 ] && exit 42\n'
        '    exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    ))
    _make_stub(bind / "git", '#!/bin/bash\necho deadbeef\n')
    return bind


def _run(stack: Path, bind: Path, *args: str, extra_env=None) -> subprocess.CompletedProcess:
    env = {
        **os.environ, "PATH": f"{bind}:{os.environ['PATH']}", "STACK_DIR": str(stack),
        "CANONICAL_DUMP": _v2_dump(),
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", str(BACKUP_SH), *args],
        env=env, capture_output=True, text=True,
    )


def _old_archive(backups: Path) -> Path:
    old = backups / "azerothcore-backup-daily-2000-01-01.tar.gz"
    old.write_bytes(b"recoverable-old-archive")
    old_t = time.time() - 30 * 86400
    os.utime(old, (old_t, old_t))
    return old


def _track_stage(bind: Path, stage: Path) -> None:
    _make_stub(
        bind / "mktemp",
        "#!/bin/sh\nmkdir -p \"$BACKUP_STAGE_DIR\"\nprintf '%s\\n' \"$BACKUP_STAGE_DIR\"\n",
    )


def _assert_prepublication_failure_preserves_recovery(
    stack: Path, stage: Path, old: Path, result: subprocess.CompletedProcess,
) -> None:
    backups = stack / "backups"
    assert result.returncode != 0
    assert old.read_bytes() == b"recoverable-old-archive"
    assert not list(backups.glob("azerothcore-backup-manual-*.tar.gz"))
    assert not list(backups.glob(".*.tmp.*"))
    assert not stage.exists()


def test_daily_backup_produces_consolidated_archive(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    r = _run(stack, bind, "--label", "daily")
    assert r.returncode == 0, r.stderr
    archives = list((stack / "backups").glob("azerothcore-backup-daily-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as tf:
        names = tf.getnames()
        assert "manifest.json" in names
        assert "sql/azerothcore.sql" in names
        assert "config/.env" in names
        assert "config/configs/modules/mod_ahbot.conf" in names
        manifest = json.loads(tf.extractfile("manifest.json").read())
    assert manifest["format_version"] == 2
    assert manifest["label"] == "daily"
    assert set(manifest["databases"]) == {
        "acore_auth", "acore_characters", "acore_world", "acore_playerbots"
    }
    assert manifest["git_revisions"]["mod-individual-progression"] in ("deadbeef", "unknown")
    assert manifest["git_revisions"]["core"] == "deadbeef"


def test_v2_backup_uses_one_transactional_multi_database_dump(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    calls = tmp_path / "docker-calls"
    env = {"DOCKER_CALLS_LOG": str(calls)}
    r = _run(stack, bind, "--label", "manual", extra_env=env)

    assert r.returncode == 0, r.stderr
    dump_calls = [line for line in calls.read_text().splitlines() if "mysqldump" in line]
    assert len(dump_calls) == 1
    assert "--single-transaction" in dump_calls[0]
    assert all(db in dump_calls[0] for db in DBS)


def test_v2_backup_passes_each_database_as_a_distinct_mysqldump_argument(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    argv_log = tmp_path / "docker-argv.nul"

    r = _run(
        stack,
        bind,
        "--label",
        "manual",
        extra_env={"DOCKER_ARGS_NUL": str(argv_log)},
    )

    assert r.returncode == 0, r.stderr
    calls = [call.split(b"\0")[:-1] for call in argv_log.read_bytes().split(b"\1") if call]
    dump = next(call for call in calls if b"mysqldump" in call)
    assert tuple(arg.decode() for arg in dump[-4:]) == DBS


def test_manual_label_uses_timestamp_and_does_not_prune(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    # Pre-seed an old archive (mtime 30 days ago) that a prune WOULD remove.
    backups = stack / "backups"
    backups.mkdir()
    old = backups / "azerothcore-backup-daily-2000-01-01.tar.gz"
    old.write_bytes(b"old")
    import time
    old_t = time.time() - 30 * 86400
    os.utime(old, (old_t, old_t))
    r = _run(stack, bind, "--label", "manual")
    assert r.returncode == 0, r.stderr
    # Manual archive is timestamped (contains 'T').
    manual = list(backups.glob("azerothcore-backup-manual-*T*.tar.gz"))
    assert len(manual) == 1
    # Manual mode does NOT prune: the old archive survives.
    assert old.exists()


def test_daily_label_prunes_all_labels_older_than_retention(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    backups = stack / "backups"
    backups.mkdir()
    import time
    old_t = time.time() - 30 * 86400
    for name in (
        "azerothcore-backup-daily-2000-01-01.tar.gz",
        "azerothcore-backup-manual-2000-01-01T00-00-00.tar.gz",
        "azerothcore-backup-prerestore-2000-01-01T00-00-00.tar.gz",
    ):
        p = backups / name
        p.write_bytes(b"old")
        os.utime(p, (old_t, old_t))
    r = _run(stack, bind, "--label", "daily")
    assert r.returncode == 0, r.stderr
    # Every old label is pruned regardless of label.
    assert not list(backups.glob("*2000-01-01*"))


def test_rejects_unknown_label(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    r = _run(stack, bind, "--label", "bogus")
    assert r.returncode == 2


def test_missing_database_aborts_without_publishing_or_pruning(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    backups = stack / "backups"; backups.mkdir()
    old = _old_archive(backups)
    stage = tmp_path / "tracked-stage"
    _track_stage(bind, stage)

    r = _run(
        stack,
        bind,
        "--label",
        "daily",
        extra_env={"FAIL_DB_NAME": "acore_world", "BACKUP_STAGE_DIR": str(stage)},
    )

    assert r.returncode == 1
    assert old.read_bytes() == b"recoverable-old-archive"
    assert list(backups.glob("azerothcore-backup-daily-*.tar.gz")) == [old]
    assert not list(backups.glob(".*.tmp.*"))
    assert not stage.exists()


def test_missing_database_container_preserves_recovery_without_creating_stage(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    backups = stack / "backups"; backups.mkdir()
    old = _old_archive(backups)

    r = _run(stack, bind, "--label", "daily", extra_env={"FAIL_DOCKER_INSPECT": "1"})

    assert r.returncode == 1
    assert old.read_bytes() == b"recoverable-old-archive"
    assert list(backups.glob("azerothcore-backup-daily-*.tar.gz")) == [old]
    assert not list(backups.glob(".*.tmp.*"))


def test_total_database_probe_outage_cleans_stage_without_publishing_or_pruning(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    backups = stack / "backups"; backups.mkdir()
    old = _old_archive(backups)
    stage = tmp_path / "tracked-stage"
    _track_stage(bind, stage)

    r = _run(
        stack,
        bind,
        "--label",
        "manual",
        extra_env={"FAIL_DB_PROBE": "1", "BACKUP_STAGE_DIR": str(stage)},
    )

    _assert_prepublication_failure_preserves_recovery(stack, stage, old, r)


def test_mysqldump_failure_cleans_stage_without_publishing_or_pruning(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    backups = stack / "backups"; backups.mkdir()
    old = _old_archive(backups)
    stage = tmp_path / "tracked-stage"
    _track_stage(bind, stage)

    r = _run(
        stack,
        bind,
        "--label",
        "manual",
        extra_env={"FAIL_MYSQLDUMP": "1", "BACKUP_STAGE_DIR": str(stage)},
    )

    _assert_prepublication_failure_preserves_recovery(stack, stage, old, r)


def test_configuration_copy_failure_cleans_stage_without_publishing_or_pruning(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    backups = stack / "backups"; backups.mkdir()
    old = _old_archive(backups)
    stage = tmp_path / "tracked-stage"
    _track_stage(bind, stage)
    _make_stub(bind / "cp", "#!/bin/sh\nexit 73\n")

    r = _run(stack, bind, "--label", "manual", extra_env={"BACKUP_STAGE_DIR": str(stage)})

    _assert_prepublication_failure_preserves_recovery(stack, stage, old, r)


def test_disk_full_while_writing_tar_cleans_temp_and_preserves_existing_archive(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    backups = stack / "backups"; backups.mkdir()
    old = _old_archive(backups)
    stage = tmp_path / "tracked-stage"
    _track_stage(bind, stage)
    _make_stub(bind / "tar", "#!/bin/sh\n[ \"$1\" = -czf ] && { printf partial > \"$2\"; exit 28; }\nexit 99\n")

    r = _run(stack, bind, "--label", "manual", extra_env={"BACKUP_STAGE_DIR": str(stage)})

    _assert_prepublication_failure_preserves_recovery(stack, stage, old, r)


def test_tar_failure_preserves_existing_daily_archive_and_no_temp_file(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    backups = stack / "backups"; backups.mkdir()
    old = backups / f"azerothcore-backup-daily-{time.strftime('%F')}.tar.gz"
    old.write_bytes(b"previous-good-archive")
    _make_stub(bind / "tar", "#!/bin/sh\nexit 42\n")

    r = _run(stack, bind, "--label", "daily")

    assert r.returncode != 0
    assert old.read_bytes() == b"previous-good-archive"
    assert not list(backups.glob(".*.tmp.*"))


def test_prune_failure_keeps_older_recovery_data_and_does_not_report_completion(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    backups = stack / "backups"; backups.mkdir()
    old = _old_archive(backups)
    _make_stub(bind / "find", "#!/bin/sh\nexit 74\n")

    r = _run(stack, bind, "--label", "daily")

    assert r.returncode != 0
    assert old.read_bytes() == b"recoverable-old-archive"
    assert "Backup complete." not in r.stdout
    assert not list(backups.glob(".*.tmp.*"))


def test_successful_daily_backup_replaces_only_via_same_directory_temp_file(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    first = _run(stack, bind, "--label", "daily")
    assert first.returncode == 0, first.stderr
    backups = stack / "backups"
    archive = next(backups.glob("azerothcore-backup-daily-*.tar.gz"))
    prior = archive.read_bytes()
    (stack / "configs" / "mysql" / "custom.cnf").write_text("[mysqld]\nchanged=1\n")
    mv_log = tmp_path / "mv-argv.nul"
    _make_stub(
        bind / "mv",
        "#!/bin/sh\nprintf '%s\\0' \"$@\" >> \"$MV_LOG\"\nexec /bin/mv \"$@\"\n",
    )

    second = _run(stack, bind, "--label", "daily", extra_env={"MV_LOG": str(mv_log)})

    assert second.returncode == 0, second.stderr
    assert archive.read_bytes() != prior
    args = mv_log.read_bytes().split(b"\0")[:-1]
    assert args[0] == b"-f"
    assert Path(args[1].decode()).parent == backups
    assert Path(args[1].decode()).name.startswith(".azerothcore-backup-daily-")
    assert b".tmp." in args[1]
    assert args[2] == str(archive).encode()
    assert not list(backups.glob(".*.tmp.*"))


def test_concurrent_backup_returns_busy_without_publishing_partial_archive(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    started, release = tmp_path / "started", tmp_path / "release"
    env = {
        "BLOCK_MYSQLDUMP": "1",
        "BACKUP_STARTED": str(started),
        "BACKUP_RELEASE": str(release),
    }
    first = subprocess.Popen(
        ["bash", str(BACKUP_SH), "--label", "manual"],
        env={**os.environ, "PATH": f"{bind}:{os.environ['PATH']}", "STACK_DIR": str(stack), "CANONICAL_DUMP": _v2_dump(), **env},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(100):
            if started.exists():
                break
            time.sleep(0.02)
        assert started.exists(), "first backup did not reach mysqldump"

        second = _run(stack, bind, "--label", "manual", extra_env=env)
        assert second.returncode == 75
        assert "another backup is already running" in second.stderr
        assert not list((stack / "backups").glob("*.tmp.*"))
    finally:
        release.touch()
        stdout, stderr = first.communicate(timeout=10)
    assert first.returncode == 0, stderr or stdout


def test_exit_zero_malformed_dump_is_not_published_or_reported_complete(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    backups = stack / "backups"
    backups.mkdir()
    old = _old_archive(backups)

    result = _run(
        stack,
        bind,
        "--label",
        "manual",
        extra_env={"MALFORMED_SUCCESS": "1", "CANONICAL_DUMP": _v2_dump()},
    )

    assert result.returncode == 1
    assert "SQL stream failed canonical validation" in result.stderr
    assert "Backup complete." not in result.stdout
    assert old.read_bytes() == b"recoverable-old-archive"
    assert not list(backups.glob("azerothcore-backup-manual-*.tar.gz"))
    assert not list(backups.glob(".*.tmp.*"))
