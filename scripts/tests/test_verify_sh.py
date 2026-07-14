import json
import io
import os
import shlex
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest


SCRIPTS_DIR = Path("/src") if Path("/src/verify-azerothcore.sh").is_file() else Path(__file__).resolve().parents[1]
VERIFY_SH = SCRIPTS_DIR / "verify-azerothcore.sh"
DBS = ("acore_auth", "acore_characters", "acore_world", "acore_playerbots")


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _stack(tmp_path: Path) -> Path:
    stack = tmp_path / "stack"
    (stack / "logs").mkdir(parents=True)
    (stack / "backups").mkdir()
    (stack / "configs" / "mysql").mkdir(parents=True)
    (stack / "configs" / "modules").mkdir()
    stack.joinpath(".env").write_text(
        "DOCKER_DB_ROOT_PASSWORD=test\n"
        "DOCKER_DB_EXTERNAL_PORT=127.0.0.1:3306\n"
        "DOCKER_SOAP_EXTERNAL_PORT=127.0.0.1:7878\n"
        "DOCKER_AUTH_EXTERNAL_PORT=100.64.0.5:3724\n"
        "DOCKER_WORLD_EXTERNAL_PORT=100.64.0.5:8085\n"
    )
    stack.joinpath("logs", "Server.log").write_text("WORLD: World Initialized\n")
    stack.joinpath("logs", "Errors.log").write_text("")
    stack.joinpath("configs", "mysql", "custom.cnf").write_text(
        "innodb_buffer_pool_size = 1G\ninnodb_buffer_pool_instances = 1\n"
    )
    stack.joinpath("configs", "modules", "mod_ahbot.conf").write_text("AuctionHouseBot.GUIDs = 1\n")
    stack.joinpath("configs", "modules", "playerbots.conf").write_text("# seeded\n")
    stack.joinpath("docker-compose.override.yml").write_text("services: {}\n")
    _executable(stack / "backup.sh", "#!/bin/sh\nexit 0\n")
    return stack


def _stubs(tmp_path: Path, *, ss_output: str = "", include_tailscale: bool = True) -> Path:
    bind = tmp_path / "bin"
    bind.mkdir(exist_ok=True)
    _executable(bind / "docker", """#!/bin/bash
printf 'docker %s\\n' "$*" >> "${VERIFY_CALL_LOG:-/dev/null}"
case "$1" in
  inspect)
    case "$*" in *ExitCode*) echo 0 ;; *StartedAt*) echo 2026-07-12T00:00:00Z ;; *) echo running ;; esac ;;
  exec) exit 0 ;;
  compose) exit 0 ;;
  images) exit 0 ;;
esac
""")
    if include_tailscale:
        _executable(bind / "tailscale", "#!/bin/sh\necho 100.64.0.5\n")
    _executable(bind / "crontab", "#!/bin/sh\necho '0 3 * * * /opt/stacks/azerothcore/backup.sh'\n")
    _executable(bind / "ss", f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(ss_output)}\n")
    _executable(bind / "systemctl", "#!/bin/sh\nexit 0\n")
    for command in ("awk", "date", "find", "grep", "gzip", "head", "ls", "paste", "python3", "sed", "sort", "tail", "tar", "tr", "wc"):
        target = shutil.which(command)
        assert target, f"test prerequisite missing: {command}"
        if not (bind / command).exists():
            (bind / command).symlink_to(target)
    return bind


def _run(stack: Path, bind: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(VERIFY_SH)],
        env={
            **os.environ,
            "STACK_DIR": str(stack),
            "PATH": str(bind),
            "VERIFY_CALL_LOG": str(bind.parent / "calls.log"),
        },
        capture_output=True,
        text=True,
    )


def _assert_complete_summary(result: subprocess.CompletedProcess[str]) -> None:
    assert "TOTAL:" in result.stdout
    counted = result.stdout.count("[OK]") + result.stdout.count("[FAIL]")
    reported = int(result.stdout.rsplit("TOTAL: ", 1)[1].split()[0])
    assert reported == counted


def _complete_archive(path: Path, *, skipped=(), footer=True) -> Path:
    stage = path.parent / "archive-stage"
    (stage / "sql").mkdir(parents=True)
    for db in DBS:
        body = "-- SQL dump\n"
        if footer:
            body += "-- Dump completed on 2026-07-12 00:00:00\n"
        (stage / "sql" / f"{db}.sql").write_text(body)
    (stage / "manifest.json").write_text(json.dumps({
        "format_version": 1, "databases": list(DBS), "skipped_databases": list(skipped),
    }))
    with tarfile.open(path, "w:gz") as tf:
        tf.add(stage / "manifest.json", arcname="manifest.json")
        tf.add(stage / "sql", arcname="sql")
    return path


def _complete_v2_archive(path: Path, *, sections=DBS, manifest=None, dump_prefix="") -> Path:
    dump = dump_prefix + "".join(
        f"-- Current Database: `{database}`\n"
        f"CREATE DATABASE `{database}`;\n"
        f"USE `{database}`;\n"
        for database in sections
    ) + "-- Dump completed on 2026-07-12 00:00:00\n"
    if manifest is None:
        manifest = json.dumps({
            "format_version": 2,
            "databases": list(DBS),
            "skipped_databases": [],
            "dump_layout": "single-multi-database",
        })
    manifest = manifest.encode() if isinstance(manifest, str) else manifest
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tf.addfile(info, io.BytesIO(manifest))
        sql = dump.encode()
        info = tarfile.TarInfo("sql/azerothcore.sql")
        info.size = len(sql)
        tf.addfile(info, io.BytesIO(sql))
    return path


def test_missing_required_env_key_reports_failure_and_summary(tmp_path):
    stack = _stack(tmp_path)
    stack.joinpath(".env").write_text("DOCKER_DB_ROOT_PASSWORD=test\n")
    result = _run(stack, _stubs(tmp_path))

    assert result.returncode == 1
    assert ".env is missing required DOCKER_AUTH_EXTERNAL_PORT" in result.stdout
    assert "unbound variable" not in result.stderr
    _assert_complete_summary(result)


def test_failure_paths_accumulate_through_malformed_bindings_and_missing_tools(tmp_path):
    stack = _stack(tmp_path)
    stack.joinpath(".env").write_text(
        "DOCKER_DB_ROOT_PASSWORD=\n"
        "DOCKER_DB_EXTERNAL_PORT=127.0.0.1:3306\n"
        "DOCKER_SOAP_EXTERNAL_PORT=not-an-address\n"
        "DOCKER_AUTH_EXTERNAL_PORT=100.64.0.5:3724\n"
        "DOCKER_WORLD_EXTERNAL_PORT=100.64.0.5:8085\n"
    )
    bind = _stubs(
        tmp_path,
        ss_output="State Recv-Q Send-Q Local Address:Port Peer Address:Port\nLISTEN 0 0 0.0.0.0:3306 0.0.0.0:*",
        include_tailscale=False,
    )

    result = _run(stack, bind)

    assert result.returncode == 1
    assert ".env is missing required DOCKER_DB_ROOT_PASSWORD" in result.stdout
    assert "tailscale binary not installed" in result.stdout
    assert "SOAP .env value is malformed (no addr:port): 'not-an-address'" in result.stdout
    assert "Port 3306 (MySQL) listening on unexpected scope: 0.0.0.0" in result.stdout
    assert "RESULT: FAIL" in result.stdout
    _assert_complete_summary(result)
    assert "docker inspect --format={{.State.Status}} ac-database" in (tmp_path / "calls.log").read_text()


def test_running_worldserver_without_current_boot_readiness_fails(tmp_path):
    stack = _stack(tmp_path)
    stack.joinpath("logs", "Server.log").write_text("booting\n")
    result = _run(stack, _stubs(tmp_path))

    assert result.returncode == 1
    assert "ac-worldserver has not reached World Initialized" in result.stdout
    _assert_complete_summary(result)


def test_backup_requires_fresh_complete_readable_v1_archive(tmp_path):
    stack = _stack(tmp_path)
    backup = _complete_archive(stack / "backups" / "azerothcore-backup-manual-2026-07-12T00-00-00.tar.gz")
    result = _run(stack, _stubs(tmp_path))

    assert "fresh complete canonical archive" in result.stdout
    _assert_complete_summary(result)

    # A valid but stale archive and corrupt/partial alternatives cannot provide
    # recovery evidence.
    old = 1
    os.utime(backup, (old, old))
    (stack / "backups" / "azerothcore-backup-daily-partial-2026-07-12.tar.gz").write_bytes(b"not a tar")
    stale = _run(stack, _stubs(tmp_path))
    assert stale.returncode == 1
    assert "none from the last 25 hours" in stale.stdout
    _assert_complete_summary(stale)


def test_backup_accepts_fresh_canonical_v2_archive(tmp_path):
    stack = _stack(tmp_path)
    _complete_v2_archive(
        stack / "backups" / "azerothcore-backup-manual-2026-07-12T00-00-00.tar.gz",
    )

    result = _run(stack, _stubs(tmp_path))

    assert "1 fresh complete canonical archive" in result.stdout
    _assert_complete_summary(result)


def test_partial_or_arbitrary_backup_does_not_count(tmp_path):
    stack = _stack(tmp_path)
    _complete_archive(
        stack / "backups" / "azerothcore-backup-daily-partial-2026-07-12.tar.gz",
        skipped=("acore_world",),
    )
    (stack / "backups" / "notes.txt").write_text("not a backup")
    result = _run(stack, _stubs(tmp_path))

    assert result.returncode == 1
    assert "no complete readable canonical archive" in result.stdout
    _assert_complete_summary(result)


def test_v2_backups_with_noncanonical_stream_sections_do_not_count(tmp_path):
    for sections in (
        DBS[:-1],
        (*DBS, "unexpected_schema"),
        (DBS[1], DBS[0], *DBS[2:]),
    ):
        stack = _stack(tmp_path / "-".join(sections))
        _complete_v2_archive(
            stack / "backups" / "azerothcore-backup-manual-2026-07-12T00-00-00.tar.gz",
            sections=sections,
        )

        result = _run(stack, _stubs(stack.parent))

        assert result.returncode == 1
        assert "no complete readable canonical archive" in result.stdout, result.stdout
        _assert_complete_summary(result)


@pytest.mark.parametrize(
    "format_values",
    [(2, 99), (99, 2)],
    ids=["canonical-then-conflicting", "conflicting-then-canonical"],
)
def test_v2_backup_with_conflicting_manifest_keys_does_not_count(tmp_path, format_values):
    stack = _stack(tmp_path)
    _complete_v2_archive(
        stack / "backups" / "azerothcore-backup-manual-2026-07-12T00-00-00.tar.gz",
        manifest=(
            f'{{"format_version":{format_values[0]},"format_version":{format_values[1]},'
            '"databases":["acore_auth","acore_characters","acore_world","acore_playerbots"],'
            '"skipped_databases":[],"dump_layout":"single-multi-database"}'
        ),
    )

    result = _run(stack, _stubs(tmp_path))

    assert "no complete readable canonical archive" in result.stdout
    _assert_complete_summary(result)


@pytest.mark.parametrize(
    "malformed_marker",
    [
        "-- Current Database: acore_auth\n",
        "-- Current Database: `acore_auth` trailing\n",
    ],
    ids=["missing-backticks", "trailing-content"],
)
def test_v2_backup_with_malformed_marker_prefixed_record_does_not_count(
    tmp_path, malformed_marker,
):
    stack = _stack(tmp_path)
    _complete_v2_archive(
        stack / "backups" / "azerothcore-backup-manual-2026-07-12T00-00-00.tar.gz",
        dump_prefix=malformed_marker,
    )

    result = _run(stack, _stubs(tmp_path))

    assert "no complete readable canonical archive" in result.stdout
    _assert_complete_summary(result)


def test_actionable_errors_log_fails_but_known_graveyard_noise_is_advisory(tmp_path):
    stack = _stack(tmp_path)
    errors = stack / "logs" / "Errors.log"
    errors.write_text("[ERROR] database connection lost\n")
    failed = _run(stack, _stubs(tmp_path))
    assert "Errors.log has actionable runtime errors" in failed.stdout
    _assert_complete_summary(failed)

    errors.write_text(
        "Table `graveyard_zone` incomplete: Zone 2037 Team 0 does not have a linked graveyard\n"
    )
    advisory = _run(stack, _stubs(tmp_path))
    assert "known graveyard_zone data-gap warning" in advisory.stdout
    _assert_complete_summary(advisory)
