import fcntl
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path("/src") if Path("/src/restore-azerothcore.sh").is_file() else Path(__file__).resolve().parents[1]
RESTORE_SH = SCRIPTS_DIR / "restore-azerothcore.sh"
DATABASES = ("acore_auth", "acore_characters", "acore_world", "acore_playerbots")


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _allow_unprivileged(path: Path) -> None:
    if os.geteuid() == 0:
        os.chown(path, 65534, 65534)
    mode = 0o777 if path.is_dir() or os.access(path, os.X_OK) else 0o666
    path.chmod(mode)
    if path.is_dir():
        for child in path.iterdir():
            _allow_unprivileged(child)


def _allow_parent_traversal(path: Path) -> None:
    for parent in path.parents:
        if parent == Path("/"):
            break
        parent.chmod(parent.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if parent == Path("/tmp"):
            break


def _make_archive(tmp_path: Path, manifest=None, sql_files=None, include_admin_compose=False) -> Path:
    stage = tmp_path / "ar"
    (stage / "sql").mkdir(parents=True)
    (stage / "config" / "configs" / "mysql").mkdir(parents=True)
    (stage / "config" / "configs" / "modules").mkdir(parents=True)
    if sql_files is None:
        sql_files = {f"{db}.sql": "-- dump --\n-- Dump completed on 2026-01-01\n" for db in DATABASES}
    for name, contents in sql_files.items():
        (stage / "sql" / name).write_text(contents)
    (stage / "config" / ".env").write_text("DOCKER_DB_ROOT_PASSWORD=ARCHIVE_OLD\n")
    (stage / "config" / "docker-compose.override.yml").write_text("services: {from: archive}\n")
    if include_admin_compose:
        (stage / "config" / "docker-compose.admin.yml").write_text("services: {admin: archive}\n")
    (stage / "config" / "configs" / "mysql" / "custom.cnf").write_text("[mysqld]\n# archive 999G\n")
    (stage / "config" / "configs" / "modules" / "mod_ahbot.conf").write_text("AuctionHouseBot.GUIDs = 100\n")
    if manifest is None:
        manifest = {
            "format_version": 1,
            "label": "manual",
            "databases": list(DATABASES),
            "skipped_databases": [],
        }
    if isinstance(manifest, bytes):
        (stage / "manifest.json").write_bytes(manifest)
    else:
        (stage / "manifest.json").write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))
    archive = tmp_path / "azerothcore-backup-manual-x.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(stage / "manifest.json", arcname="manifest.json")
        tf.add(stage / "sql", arcname="sql")
        tf.add(stage / "config", arcname="config")
    return archive


def _make_traversal_archive(tmp_path: Path) -> Path:
    stage = tmp_path / "bad"
    stage.mkdir()
    (stage / "manifest.json").write_text(json.dumps({"format_version": 1}))
    archive = tmp_path / "azerothcore-backup-manual-bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(stage / "manifest.json", arcname="manifest.json")
        info = tarfile.TarInfo("../outside.txt")
        payload = b"outside"
        info.size = len(payload)
        import io
        tf.addfile(info, io.BytesIO(payload))
    return archive


def _make_link_archive(tmp_path: Path) -> Path:
    archive = _make_archive(tmp_path)
    rewritten = tmp_path / "azerothcore-backup-manual-link.tar.gz"
    with tarfile.open(archive, "r:gz") as source, tarfile.open(rewritten, "w:gz") as target:
        for member in source.getmembers():
            extracted = source.extractfile(member) if member.isfile() else None
            target.addfile(member, extracted)
        link = tarfile.TarInfo("config/unsafe-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../../../outside"
        target.addfile(link)
    return rewritten


def _stack(tmp_path: Path) -> Path:
    stack = tmp_path / "stack"
    (stack / "configs" / "mysql").mkdir(parents=True)
    (stack / "configs" / "modules").mkdir(parents=True)
    stack.joinpath(".env").write_text("DOCKER_DB_ROOT_PASSWORD=FRESH_NEW\n")
    stack.joinpath("docker-compose.override.yml").write_text("services: {from: fresh}\n")
    stack.joinpath("configs", "mysql", "custom.cnf").write_text("[mysqld]\n# fresh 2G\n")
    return stack


def _run(stack, archive, bind, logf, extra_env=None):
    if os.geteuid() == 0:
        _allow_parent_traversal(bind.parent)
        _allow_unprivileged(stack)
        _allow_unprivileged(archive)
        _allow_unprivileged(bind.parent)
        preexec_fn = lambda: (os.setgid(65534), os.setuid(65534))
    else:
        preexec_fn = None
    env = {
        **os.environ,
        "PATH": f"{bind}:{os.environ['PATH']}",
        "DOCKER_CALLS_LOG": str(logf),
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", str(RESTORE_SH), str(archive), "--stack-dir", str(stack), "--yes"],
        env=env, capture_output=True, text=True, preexec_fn=preexec_fn,
    )


def _v2_sql_stream(databases=DATABASES):
    sections = "\n".join(
        f"-- Current Database: `{database}`\n"
        f"CREATE DATABASE `{database}`;\n"
        f"USE `{database}`;"
        for database in databases
    )
    return f"-- MySQL dump 10.13\n{sections}\n-- Dump completed on 2026-01-01\n"


def _stateful_docker_stub(path: Path) -> Path:
    docker = path / "docker"
    _make_stub(docker, (
        '#!/bin/bash\n'
        'echo "$@" >> "$DOCKER_CALLS_LOG"\n'
        'state_file="$DOCKER_STATE_FILE"\n'
        'state="$(cat "$state_file" 2>/dev/null || echo running)"\n'
        'case "$1" in\n'
        '  inspect)\n'
        '    if printf "%s " "$@" | grep -q "StartedAt"; then echo "2026-07-12T00:00:00Z"; else echo "$state"; fi\n'
        '    exit 0 ;;\n'
        '  info) exit 0 ;;\n'
        '  stop)\n'
        '    if [ "${DOCKER_STOP_FAIL:-0}" = 1 ]; then exit 42; fi\n'
        '    echo exited > "$state_file"; exit 0 ;;\n'
        '  compose) echo running > "$state_file"; exit 0 ;;\n'
        '  logs) echo "WORLD: World Initialized"; exit 0 ;;\n'
        '  exec)\n'
        '    if printf "%s " "$@" | grep -q "SELECT address"; then echo "100.64.0.5"; fi\n'
        '    exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    ))
    return docker


def _failure_injection_stubs(path: Path) -> None:
    _make_stub(path / "docker", (
        '#!/bin/bash\n'
        'echo "$@" >> "$DOCKER_CALLS_LOG"\n'
        'state_file="$DOCKER_STATE_FILE"\n'
        'state="$(cat "$state_file" 2>/dev/null || echo running)"\n'
        'record() { echo "$1" >> "$MUTATION_LOG"; }\n'
        'case "$1" in\n'
        '  inspect)\n'
        '    if printf "%s " "$@" | grep -q "StartedAt"; then\n'
        '      [ "${FAIL_STARTED_AT:-0}" = 1 ] || echo "2026-07-12T00:00:00Z"\n'
        '    else echo "$state"; fi\n'
        '    exit 0 ;;\n'
        '  info) exit 0 ;;\n'
        '  stop)\n'
        '    record stop\n'
        '    [ "${FAIL_STOP:-0}" != 1 ] || exit 42\n'
        '    echo exited > "$state_file"; exit 0 ;;\n'
        '  compose)\n'
        '    record recreate\n'
        '    [ "${FAIL_RECREATE:-0}" != 1 ] || exit 42\n'
        '    echo "${POST_RECREATE_STATE:-running}" > "$state_file"; exit 0 ;;\n'
        '  logs)\n'
        '    [ "${FAIL_READY_LOGS:-0}" != 1 ] && echo "WORLD: World Initialized"\n'
        '    exit 0 ;;\n'
        '  exec)\n'
        '    if printf "%s " "$@" | grep -q "SELECT address"; then\n'
        '      record realmlist-read; echo "100.64.0.5"; exit 0\n'
        '    fi\n'
        '    if printf "%s " "$@" | grep -q "DROP DATABASE"; then\n'
        '      count_file="$MUTATION_LOG.replace-count"\n'
        '      count="$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))"\n'
        '      echo "$count" > "$count_file"; record "drop:$count"\n'
        '      [ "${FAIL_DROP_AT:-0}" != "$count" ] || exit 42\n'
        '      record "create:$count"\n'
        '      [ "${FAIL_CREATE_AT:-0}" != "$count" ] || exit 42\n'
        '      exit 0\n'
        '    fi\n'
        '    if printf "%s " "$@" | grep -q " -i "; then\n'
        '      count_file="$MUTATION_LOG.import-count"\n'
        '      count="$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))"\n'
        '      echo "$count" > "$count_file"; record "import:$count"\n'
        '      [ "${FAIL_IMPORT_AT:-0}" != "$count" ] || exit 42\n'
        '      exit 0\n'
        '    fi\n'
        '    if printf "%s " "$@" | grep -q "UPDATE acore_auth.realmlist"; then record realmlist-write; fi\n'
        '    exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    ))
    _make_stub(path / "cp", (
        '#!/bin/bash\n'
        'count_file="$MUTATION_LOG.copy-count"\n'
        'count="$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))"\n'
        'echo "$count" > "$count_file"\n'
        'echo "copy:$count" >> "$MUTATION_LOG"\n'
        'if [ "${FAIL_COPY_AT:-0}" = "$count" ]; then exit 42; fi\n'
        'exec /bin/cp "$@"\n'
    ))


def _failure_run(tmp_path, monkeypatch, *, archive=None, **extra_env):
    stack = _stack(tmp_path)
    archive = archive or _make_archive(tmp_path)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    mutation_log = tmp_path / "mutations.log"
    state = tmp_path / "worldserver-state"; state.write_text("running\n")
    _failure_injection_stubs(bind)
    monkeypatch.setenv("DOCKER_STATE_FILE", str(state))
    return stack, _run(
        stack, archive, bind, logf,
        {"MUTATION_LOG": str(mutation_log), **{key: str(value) for key, value in extra_env.items()}},
    ), mutation_log, logf


def _mutations(path: Path):
    return path.read_text().splitlines() if path.exists() else []


def _successful_database_mutations():
    return [
        mutation
        for ordinal in range(1, len(DATABASES) + 1)
        for mutation in (f"drop:{ordinal}", f"create:{ordinal}", f"import:{ordinal}")
    ]


def test_dr_restore_rejects_incomplete_dumps_before_any_mutation(tmp_path, monkeypatch):
    archive = _make_archive(
        tmp_path,
        sql_files={
            f"{db}.sql": "-- dump --\n-- Dump completed on 2026-01-01\n"
            for db in DATABASES
        } | {"acore_characters.sql": "-- dump without completion marker\n"},
    )

    stack, result, mutations, _ = _failure_run(tmp_path, monkeypatch, archive=archive)

    assert result.returncode == 1
    assert "acore_characters(incomplete)" in result.stderr
    assert _mutations(mutations) == []
    assert "from: fresh" in stack.joinpath("docker-compose.override.yml").read_text()


def test_dr_restore_stop_failure_leaves_the_fresh_stack_untouched(tmp_path, monkeypatch):
    stack, result, mutations, _ = _failure_run(tmp_path, monkeypatch, FAIL_STOP=1)

    assert result.returncode == 1
    assert "Could not stop ac-worldserver" in result.stderr
    assert _mutations(mutations) == ["realmlist-read", "stop"]
    assert "from: fresh" in stack.joinpath("docker-compose.override.yml").read_text()


@pytest.mark.parametrize("ordinal", range(1, 5))
def test_dr_restore_each_config_copy_failure_preserves_the_stopped_recovery_state(
    tmp_path, monkeypatch, ordinal,
):
    stack, result, mutations, _ = _failure_run(tmp_path, monkeypatch, FAIL_COPY_AT=ordinal)

    assert result.returncode == 1
    assert "Could not copy restored configuration files" in result.stderr
    assert "server remains stopped" in result.stderr
    assert _mutations(mutations) == [
        "realmlist-read", "stop", *[f"copy:{index}" for index in range(1, ordinal + 1)],
    ]
    assert "from: fresh" in stack.joinpath("docker-compose.override.yml").read_text()
    assert "fresh 2G" in stack.joinpath("configs/mysql/custom.cnf").read_text()
    assert "DROP DATABASE" not in (mutations.read_text() if mutations.exists() else "")


def test_dr_restore_admin_compose_copy_failure_stops_before_database_replacement(tmp_path, monkeypatch):
    archive = _make_archive(tmp_path, include_admin_compose=True)
    stack, result, mutations, _ = _failure_run(tmp_path, monkeypatch, archive=archive, FAIL_COPY_AT=5)

    assert result.returncode == 1
    assert "Could not copy restored configuration files" in result.stderr
    assert "server remains stopped" in result.stderr
    assert _mutations(mutations) == [
        "realmlist-read", "stop", "copy:1", "copy:2", "copy:3", "copy:4", "copy:5",
    ]
    assert not stack.joinpath("docker-compose.admin.yml").exists()


@pytest.mark.parametrize("ordinal", range(1, len(DATABASES) + 1))
def test_dr_restore_drop_failure_stops_at_the_failing_ordinal(tmp_path, monkeypatch, ordinal):
    _, result, mutations, _ = _failure_run(tmp_path, monkeypatch, FAIL_DROP_AT=ordinal)

    assert result.returncode == 1
    assert f"Could not drop and recreate {DATABASES[ordinal - 1]}" in result.stderr
    assert "server remains stopped" in result.stderr
    assert _mutations(mutations) == [
        "realmlist-read", "stop", "copy:1", "copy:2", "copy:3", "copy:4",
        *[
            mutation
            for index in range(1, ordinal)
            for mutation in (f"drop:{index}", f"create:{index}", f"import:{index}")
        ],
        f"drop:{ordinal}",
    ]


@pytest.mark.parametrize("ordinal", range(1, len(DATABASES) + 1))
def test_dr_restore_create_failure_follows_a_successful_drop_at_each_ordinal(tmp_path, monkeypatch, ordinal):
    _, result, mutations, _ = _failure_run(tmp_path, monkeypatch, FAIL_CREATE_AT=ordinal)

    assert result.returncode == 1
    assert f"Could not drop and recreate {DATABASES[ordinal - 1]}" in result.stderr
    assert "server remains stopped" in result.stderr
    assert _mutations(mutations) == [
        "realmlist-read", "stop", "copy:1", "copy:2", "copy:3", "copy:4",
        *[
            mutation
            for index in range(1, ordinal)
            for mutation in (f"drop:{index}", f"create:{index}", f"import:{index}")
        ],
        f"drop:{ordinal}", f"create:{ordinal}",
    ]


@pytest.mark.parametrize("ordinal", range(1, len(DATABASES) + 1))
def test_dr_restore_import_failure_preserves_the_stopped_recovery_state(tmp_path, monkeypatch, ordinal):
    _, result, mutations, _ = _failure_run(tmp_path, monkeypatch, FAIL_IMPORT_AT=ordinal)

    assert result.returncode == 1
    assert f"Import of {DATABASES[ordinal - 1]} failed" in result.stderr
    assert "server remains stopped" in result.stderr
    assert _mutations(mutations) == [
        "realmlist-read", "stop", "copy:1", "copy:2", "copy:3", "copy:4",
        *[
            mutation
            for index in range(1, ordinal + 1)
            for mutation in (f"drop:{index}", f"create:{index}", f"import:{index}")
        ],
    ]


@pytest.mark.parametrize(
    ("failure_env", "expected_error"),
    [
        ({"FAIL_RECREATE": 1}, "Could not recreate ac-worldserver"),
        ({"FAIL_STARTED_AT": 1}, "Could not determine the recreated ac-worldserver start time"),
        ({"POST_RECREATE_STATE": "exited"}, "ac-worldserver entered exited during restore startup"),
        (
            {"FAIL_READY_LOGS": 1, "RESTORE_READY_TIMEOUT_SECONDS": 0, "RESTORE_READY_POLL_SECONDS": 1},
            "ac-worldserver did not reach World Initialized within 0s",
        ),
    ],
)
def test_dr_restore_recreate_and_readiness_failures_preserve_recovery_state(
    tmp_path, monkeypatch, failure_env, expected_error,
):
    _, result, mutations, _ = _failure_run(tmp_path, monkeypatch, **failure_env)

    assert result.returncode == 1
    assert expected_error in result.stderr
    assert _mutations(mutations) == [
        "realmlist-read", "stop", "copy:1", "copy:2", "copy:3", "copy:4",
        *_successful_database_mutations(),
        "realmlist-write", "recreate",
    ]


def test_dr_restore_uses_the_fresh_environment_when_recreating_the_server(tmp_path, monkeypatch):
    stack, result, mutations, logf = _failure_run(tmp_path, monkeypatch)

    assert result.returncode == 0, result.stderr
    assert "FRESH_NEW" in stack.joinpath(".env").read_text()
    assert "ARCHIVE_OLD" not in stack.joinpath(".env").read_text()
    assert "mysql -uroot -pFRESH_NEW" in logf.read_text()
    assert _mutations(mutations) == [
        "realmlist-read", "stop", "copy:1", "copy:2", "copy:3", "copy:4",
        *_successful_database_mutations(),
        "realmlist-write", "recreate",
    ]


def test_dr_restore_preserves_env_and_custom_cnf_and_imports(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    _make_stub(bind / "docker", (
        '#!/bin/bash\n'
        'echo "$@" >> "$DOCKER_CALLS_LOG"\n'
        'state_file="$DOCKER_CALLS_LOG.state"\n'
        'if [ "$1" = inspect ]; then if printf "%s " "$@" | grep -q "StartedAt"; then echo "2026-07-12T00:00:00Z"; elif [ -f "$state_file" ] && grep -q stopped "$state_file"; then echo exited; else echo running; fi; exit 0; fi\n'
        'if [ "$1" = stop ]; then echo stopped > "$state_file"; exit 0; fi\n'
        'if [ "$1" = compose ]; then rm -f "$state_file"; exit 0; fi\n'
        'if [ "$1" = logs ]; then echo "WORLD: World Initialized"; exit 0; fi\n'
        'if printf "%s " "$@" | grep -q "SELECT address"; then echo "100.64.0.5"; fi\n'
        'exit 0\n'
    ))
    r = _run(stack, archive, bind, logf)
    assert r.returncode == 0, r.stderr

    # .env is NOT overwritten (fresh password kept).
    assert "FRESH_NEW" in (stack / ".env").read_text()
    # custom.cnf is NOT overwritten (machine-tuned fresh kept).
    assert "fresh 2G" in (stack / "configs" / "mysql" / "custom.cnf").read_text()
    # override.yml + mod_ahbot.conf ARE restored from the archive.
    assert "from: archive" in (stack / "docker-compose.override.yml").read_text()
    assert (stack / "configs" / "modules" / "mod_ahbot.conf").read_text().strip().endswith("100")

    calls = logf.read_text()
    # Each DB dropped/created + realmlist re-applied with the captured address.
    assert "DROP DATABASE IF EXISTS acore_characters" in calls
    assert "UPDATE acore_auth.realmlist SET address='100.64.0.5'" in calls


def test_dr_restore_does_not_create_custom_cnf_from_archive(tmp_path):
    stack = _stack(tmp_path)
    stack.joinpath("configs", "mysql", "custom.cnf").unlink()
    archive = _make_archive(tmp_path)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    _make_stub(bind / "docker", (
        '#!/bin/bash\n'
        'echo "$@" >> "$DOCKER_CALLS_LOG"\n'
        'state_file="$DOCKER_CALLS_LOG.state"\n'
        'if [ "$1" = inspect ]; then if printf "%s " "$@" | grep -q "StartedAt"; then echo "2026-07-12T00:00:00Z"; elif [ -f "$state_file" ] && grep -q stopped "$state_file"; then echo exited; else echo running; fi; exit 0; fi\n'
        'if [ "$1" = stop ]; then echo stopped > "$state_file"; exit 0; fi\n'
        'if [ "$1" = compose ]; then rm -f "$state_file"; exit 0; fi\n'
        'if [ "$1" = logs ]; then echo "WORLD: World Initialized"; exit 0; fi\n'
        'if printf "%s " "$@" | grep -q "SELECT address"; then echo "100.64.0.5"; fi\n'
        'exit 0\n'
    ))

    r = _run(stack, archive, bind, logf)

    assert r.returncode == 0, r.stderr
    assert not (stack / "configs" / "mysql" / "custom.cnf").exists()
    assert "from: archive" in (stack / "docker-compose.override.yml").read_text()


def test_dr_restore_aborts_before_copying_configs_when_realmlist_capture_fails(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    _make_stub(bind / "docker", (
        '#!/bin/bash\n'
        'echo "$@" >> "$DOCKER_CALLS_LOG"\n'
        'if printf "%s " "$@" | grep -q "SELECT address"; then exit 42; fi\n'
        'exit 0\n'
    ))

    r = _run(stack, archive, bind, logf)

    assert r.returncode == 1
    assert "Unable to read fresh realmlist address" in r.stderr
    assert "from: fresh" in (stack / "docker-compose.override.yml").read_text()
    calls = logf.read_text()
    assert "stop ac-worldserver" not in calls
    assert "DROP DATABASE IF EXISTS" not in calls


def test_dr_restore_aborts_without_mutation_when_worldserver_stop_fails(tmp_path, monkeypatch):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    state = tmp_path / "worldserver-state"; state.write_text("running\n")
    _stateful_docker_stub(bind)
    monkeypatch.setenv("DOCKER_STATE_FILE", str(state))
    monkeypatch.setenv("DOCKER_STOP_FAIL", "1")

    r = _run(stack, archive, bind, logf)

    assert r.returncode == 1
    assert "Could not stop ac-worldserver" in r.stderr
    assert "from: fresh" in (stack / "docker-compose.override.yml").read_text()
    calls = logf.read_text()
    assert "DROP DATABASE IF EXISTS" not in calls


def test_dr_restore_accepts_an_already_exited_worldserver(tmp_path, monkeypatch):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    state = tmp_path / "worldserver-state"; state.write_text("exited\n")
    _stateful_docker_stub(bind)
    monkeypatch.setenv("DOCKER_STATE_FILE", str(state))

    r = _run(stack, archive, bind, logf)

    assert r.returncode == 0, r.stderr
    assert "stop --time" not in logf.read_text()


def test_dr_restore_confirms_worldserver_stopped_before_mutation(tmp_path, monkeypatch):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    state = tmp_path / "worldserver-state"; state.write_text("running\n")
    _stateful_docker_stub(bind)
    monkeypatch.setenv("DOCKER_STATE_FILE", str(state))

    r = _run(stack, archive, bind, logf)

    assert r.returncode == 0, r.stderr
    calls = logf.read_text()
    assert "stop --time 60 ac-worldserver" in calls
    assert calls.index("stop --time 60 ac-worldserver") < calls.index("DROP DATABASE IF EXISTS")
    assert "compose up -d --force-recreate --no-deps ac-worldserver" in calls
    assert "start ac-worldserver" not in calls


def test_dr_restore_rejects_archive_path_traversal_before_extract(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_traversal_archive(tmp_path)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    _make_stub(bind / "docker", (
        '#!/bin/bash\n'
        'echo "$@" >> "$DOCKER_CALLS_LOG"\n'
        'exit 0\n'
    ))

    r = _run(stack, archive, bind, logf)

    assert r.returncode == 1
    assert "unsafe archive member" in r.stderr
    assert not (tmp_path / "outside.txt").exists()
    assert not logf.exists()


@pytest.mark.parametrize(
    ("manifest", "sql_files", "expected_error"),
    [
        ("{not-json", None, "not valid JSON"),
        (b'\xff', None, "not valid UTF-8"),
        ({"format_version": 99}, None, "not supported"),
        ({"format_version": 1}, None, "database inventory is missing"),
        (
            {"format_version": 1, "databases": list(DATABASES[:-1]), "skipped_databases": []},
            None,
            "database inventory",
        ),
        (
            {"format_version": 1, "databases": list(DATABASES), "skipped_databases": ["acore_world"]},
            None,
            "skipped databases",
        ),
        (
            {
                "format_version": 2,
                "databases": list(DATABASES),
                "skipped_databases": [],
                "dump_layout": "per-database",
            },
            {"azerothcore.sql": "-- dump --\n-- Dump completed on 2026-01-01\n"},
            "dump_layout",
        ),
    ],
)
def test_dr_restore_rejects_incompatible_manifest_before_stop_or_mutation(
    tmp_path, manifest, sql_files, expected_error, monkeypatch,
):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path, manifest=manifest, sql_files=sql_files)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    state = tmp_path / "worldserver-state"; state.write_text("running\n")
    _stateful_docker_stub(bind)
    monkeypatch.setenv("DOCKER_STATE_FILE", str(state))

    r = _run(stack, archive, bind, logf)

    assert r.returncode == 1
    assert expected_error in r.stderr
    assert "from: fresh" in (stack / "docker-compose.override.yml").read_text()
    calls = logf.read_text() if logf.exists() else ""
    assert "stop --time" not in calls
    assert "SELECT address" not in calls
    assert "DROP DATABASE IF EXISTS" not in calls


def test_dr_restore_accepts_complete_v2_multi_database_manifest(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_archive(
        tmp_path,
        manifest={
            "format_version": 2,
            "databases": list(DATABASES),
            "skipped_databases": [],
            "dump_layout": "single-multi-database",
        },
        sql_files={"azerothcore.sql": _v2_sql_stream()},
    )
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    _make_stub(bind / "docker", (
        '#!/bin/bash\n'
        'echo "$@" >> "$DOCKER_CALLS_LOG"\n'
        'state_file="$DOCKER_CALLS_LOG.state"\n'
        'if [ "$1" = inspect ]; then if printf "%s " "$@" | grep -q "StartedAt"; then echo "2026-07-12T00:00:00Z"; elif [ -f "$state_file" ] && grep -q stopped "$state_file"; then echo exited; else echo running; fi; exit 0; fi\n'
        'if [ "$1" = stop ]; then echo stopped > "$state_file"; exit 0; fi\n'
        'if [ "$1" = compose ]; then rm -f "$state_file"; exit 0; fi\n'
        'if [ "$1" = logs ]; then echo "WORLD: World Initialized"; exit 0; fi\n'
        'if printf "%s " "$@" | grep -q "SELECT address"; then echo "100.64.0.5"; fi\n'
        'exit 0\n'
    ))

    r = _run(stack, archive, bind, logf)

    assert r.returncode == 0, r.stderr
    assert "Restoring v2 consistent multi-database snapshot" in r.stdout
    calls = logf.read_text()
    assert "DROP DATABASE IF EXISTS acore_playerbots" in calls
    assert "mysql -uroot -pFRESH_NEW" in calls


@pytest.mark.parametrize(
    ("stream_databases", "expected_error"),
    [
        (DATABASES[:-1], "database sections"),
        (DATABASES + ("unexpected_schema",), "database sections"),
    ],
)
def test_dr_restore_rejects_v2_stream_with_noncanonical_database_sections_before_mutation(
    tmp_path, stream_databases, expected_error, monkeypatch,
):
    stack = _stack(tmp_path)
    archive = _make_archive(
        tmp_path,
        manifest={
            "format_version": 2,
            "databases": list(DATABASES),
            "skipped_databases": [],
            "dump_layout": "single-multi-database",
        },
        sql_files={"azerothcore.sql": _v2_sql_stream(stream_databases)},
    )
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    state = tmp_path / "worldserver-state"; state.write_text("running\n")
    _stateful_docker_stub(bind)
    monkeypatch.setenv("DOCKER_STATE_FILE", str(state))

    r = _run(stack, archive, bind, logf)

    assert r.returncode == 1
    assert expected_error in r.stderr
    assert "from: fresh" in (stack / "docker-compose.override.yml").read_text()
    calls = logf.read_text() if logf.exists() else ""
    assert "SELECT address" not in calls
    assert "stop --time" not in calls
    assert "DROP DATABASE IF EXISTS" not in calls


def test_dr_restore_aborts_before_copying_configs_when_realmlist_capture_is_empty(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    _make_stub(bind / "docker", (
        '#!/bin/bash\n'
        'echo "$@" >> "$DOCKER_CALLS_LOG"\n'
        'exit 0\n'
    ))

    r = _run(stack, archive, bind, logf)

    assert r.returncode == 1
    assert "Fresh realmlist address is empty" in r.stderr
    assert "from: fresh" in (stack / "docker-compose.override.yml").read_text()
    calls = logf.read_text()
    assert "stop ac-worldserver" not in calls
    assert "DROP DATABASE IF EXISTS" not in calls


def test_dr_restore_refuses_root_even_with_custom_stack_dir(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    _make_stub(bind / "docker", (
        '#!/bin/bash\n'
        'echo "$@" >> "$DOCKER_CALLS_LOG"\n'
        'exit 0\n'
    ))
    env = {**os.environ, "PATH": f"{bind}:{os.environ['PATH']}", "DOCKER_CALLS_LOG": str(logf)}

    r = subprocess.run(
        ["bash", str(RESTORE_SH), str(archive), "--stack-dir", str(stack), "--yes"],
        env=env, capture_output=True, text=True,
    )

    if os.geteuid() == 0:
        assert r.returncode == 1
        assert "Do not run this script with sudo/root" in r.stderr
    else:
        assert r.returncode == 0, r.stderr


def test_dr_restore_refuses_root_before_help():
    r = subprocess.run(
        ["bash", str(RESTORE_SH), "--help"],
        capture_output=True, text=True,
    )

    if os.geteuid() == 0:
        assert r.returncode == 1
        assert "Do not run this script with sudo/root" in r.stderr
        assert "Usage:" not in r.stdout
    else:
        assert r.returncode == 0, r.stderr


def test_dr_restore_rejects_links_before_docker_or_mutation(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_link_archive(tmp_path)
    bind = tmp_path / "bin"
    bind.mkdir()
    logf = tmp_path / "docker.log"
    _stateful_docker_stub(bind)

    result = _run(stack, archive, bind, logf)

    assert result.returncode == 1
    assert "unsupported archive member type" in result.stderr
    assert not logf.exists()


def test_dr_restore_cleans_every_tmpdir_artifact_after_success(tmp_path, monkeypatch):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    bind = tmp_path / "bin"
    bind.mkdir()
    logf = tmp_path / "docker.log"
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    state = tmp_path / "worldserver-state"
    state.write_text("running\n")
    _stateful_docker_stub(bind)
    monkeypatch.setenv("DOCKER_STATE_FILE", str(state))

    result = _run(stack, archive, bind, logf, {"TMPDIR": str(tmpdir)})

    assert result.returncode == 0, result.stderr
    assert list(tmpdir.iterdir()) == []


def test_dr_restore_refuses_backup_lock_contention_before_mutation(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    backups = stack / "backups"
    backups.mkdir()
    lock_path = backups / ".backup.lock"
    lock_path.touch()
    bind = tmp_path / "bin"
    bind.mkdir()
    logf = tmp_path / "docker.log"
    _stateful_docker_stub(bind)

    with lock_path.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run(stack, archive, bind, logf)

    assert result.returncode == 75
    assert "backup or restore is already running" in result.stderr
    assert not logf.exists()
    assert "from: fresh" in (stack / "docker-compose.override.yml").read_text()
