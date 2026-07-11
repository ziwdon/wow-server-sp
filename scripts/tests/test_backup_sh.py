import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path

SCRIPTS_DIR = Path("/src") if Path("/src/backup.sh").is_file() else Path(__file__).resolve().parents[1]
BACKUP_SH = SCRIPTS_DIR / "backup.sh"


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
        'case "$1" in\n'
        '  inspect) exit 0 ;;\n'
        '  exec)\n'
        '    shift\n'
        '    if printf "%s " "$@" | grep -q mysqldump; then echo "-- dump --"; exit 0; fi\n'
        '    # `mysql -e "USE db"` existence probe — succeed for all four.\n'
        '    exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    ))
    _make_stub(bind / "git", '#!/bin/bash\necho deadbeef\n')
    return bind


def _run(stack: Path, bind: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{bind}:{os.environ['PATH']}", "STACK_DIR": str(stack)}
    return subprocess.run(
        ["bash", str(BACKUP_SH), *args],
        env=env, capture_output=True, text=True,
    )


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
        assert "sql/acore_auth.sql" in names
        assert "sql/acore_world.sql" in names
        assert "config/.env" in names
        assert "config/configs/modules/mod_ahbot.conf" in names
        manifest = json.loads(tf.extractfile("manifest.json").read())
    assert manifest["format_version"] == 1
    assert manifest["label"] == "daily"
    assert set(manifest["databases"]) == {
        "acore_auth", "acore_characters", "acore_world", "acore_playerbots"
    }
    assert manifest["git_revisions"]["mod-individual-progression"] in ("deadbeef", "unknown")
    assert manifest["git_revisions"]["core"] == "deadbeef"


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


def test_partial_backup_is_labeled_and_does_not_prune(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    docker = bind / "docker"
    docker.write_text(docker.read_text().replace(
        "    # `mysql -e \"USE db\"` existence probe — succeed for all four.\n    exit 0 ;;",
        "    if printf '%s ' \"$@\" | grep -q 'USE acore_world'; then exit 1; fi\n    exit 0 ;;",
    ))
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    backups = stack / "backups"; backups.mkdir()
    old = backups / "azerothcore-backup-daily-2000-01-01.tar.gz"; old.write_bytes(b"old")
    r = _run(stack, bind, "--label", "daily")
    assert r.returncode == 1
    assert list(backups.glob("azerothcore-backup-daily-partial-*.tar.gz"))
    assert old.exists()
