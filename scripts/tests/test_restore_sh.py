import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "scripts" / "restore-azerothcore.sh"


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


def _make_archive(tmp_path: Path) -> Path:
    stage = tmp_path / "ar"
    (stage / "sql").mkdir(parents=True)
    (stage / "config" / "configs" / "mysql").mkdir(parents=True)
    (stage / "config" / "configs" / "modules").mkdir(parents=True)
    for db in ("acore_auth", "acore_characters", "acore_world", "acore_playerbots"):
        (stage / "sql" / f"{db}.sql").write_text("-- dump --")
    (stage / "config" / ".env").write_text("DOCKER_DB_ROOT_PASSWORD=ARCHIVE_OLD\n")
    (stage / "config" / "docker-compose.override.yml").write_text("services: {from: archive}\n")
    (stage / "config" / "configs" / "mysql" / "custom.cnf").write_text("[mysqld]\n# archive 999G\n")
    (stage / "config" / "configs" / "modules" / "mod_ahbot.conf").write_text("AuctionHouseBot.GUIDs = 100\n")
    (stage / "manifest.json").write_text(json.dumps({"format_version": 1, "label": "manual"}))
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


def _stack(tmp_path: Path) -> Path:
    stack = tmp_path / "stack"
    (stack / "configs" / "mysql").mkdir(parents=True)
    (stack / "configs" / "modules").mkdir(parents=True)
    stack.joinpath(".env").write_text("DOCKER_DB_ROOT_PASSWORD=FRESH_NEW\n")
    stack.joinpath("docker-compose.override.yml").write_text("services: {from: fresh}\n")
    stack.joinpath("configs", "mysql", "custom.cnf").write_text("[mysqld]\n# fresh 2G\n")
    return stack


def _run(stack, archive, bind, logf):
    if os.geteuid() == 0:
        _allow_parent_traversal(bind.parent)
        _allow_unprivileged(stack)
        _allow_unprivileged(archive)
        _allow_unprivileged(bind.parent)
        preexec_fn = lambda: (os.setgid(65534), os.setuid(65534))
    else:
        preexec_fn = None
    env = {**os.environ, "PATH": f"{bind}:{os.environ['PATH']}", "DOCKER_CALLS_LOG": str(logf)}
    return subprocess.run(
        ["bash", str(RESTORE_SH), str(archive), "--stack-dir", str(stack), "--yes"],
        env=env, capture_output=True, text=True, preexec_fn=preexec_fn,
    )


def test_dr_restore_preserves_env_and_custom_cnf_and_imports(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    bind = tmp_path / "bin"; bind.mkdir()
    logf = tmp_path / "docker.log"
    _make_stub(bind / "docker", (
        '#!/bin/bash\n'
        'echo "$@" >> "$DOCKER_CALLS_LOG"\n'
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
    assert "Unsafe archive member" in r.stderr
    assert not (tmp_path / "outside.txt").exists()
    assert not logf.exists()


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
