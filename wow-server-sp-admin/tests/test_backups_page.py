import datetime as dt
import io
import json
import os
from pathlib import Path
import tarfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    (tmp_path / "backups").mkdir()
    (tmp_path / "logs").mkdir()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "worldserver.conf.dist").write_text("")
    (dist / "playerbots.conf.dist").write_text("")
    (dist / "mod_ahbot.conf.dist").write_text("")
    (dist / "individualProgression.conf.dist").write_text("")
    from app.state import init_state

    init_state(
        dist_dir=Path("/app/dist") if Path("/app/dist").exists() else dist,
        admin_yml=tmp_path / "docker-compose.admin.yml",
        override_yml=tmp_path / "docker-compose.override.yml",
        configs_dir=tmp_path / "configs",
        snapshots_dir=tmp_path / "snap",
    )
    from app.main import app

    return TestClient(app)


def test_backups_page_renders(client):
    r = client.get("/backups")
    assert r.status_code == 200
    assert "Backups" in r.text
    assert 'data-action-endpoint="/api/action/backup"' in r.text
    assert 'id="restore-btn"' in r.text
    assert "Restore selected" in r.text
    assert "/static/backups.js?v=" in r.text


def test_nav_has_backups_link(client):
    r = client.get("/")
    assert 'href="/backups"' in r.text


def test_backups_list_endpoint(client):
    r = client.get("/api/backups/list")
    assert r.status_code == 200


def test_backups_summary_endpoint(client):
    r = client.get("/api/backups/summary")
    assert r.status_code == 200
    assert "Last Backup" in r.text
    assert "Available Backups" in r.text
    assert "Disk Used" in r.text


def test_backups_endpoints_list_matching_non_tar_as_available(client, tmp_path):
    archive = tmp_path / "backups" / "azerothcore-backup-manual-2026-05-29T14-03-10.tar.gz"
    archive.write_bytes(b"not a gzip archive")
    # The list now shows the archive's real write time (mtime), not the filename
    # stamp. Pin a known mtime so the rendered time is deterministic.
    when = dt.datetime(2026, 5, 29, 14, 3, 10, tzinfo=dt.timezone.utc).timestamp()
    os.utime(archive, (when, when))

    summary = client.get("/api/backups/summary")
    listing = client.get("/api/backups/list")

    assert summary.status_code == 200
    assert listing.status_code == 200
    assert "Available" in summary.text
    assert "Available" in listing.text
    assert 'class="backup-row backup-available"' in listing.text
    assert 'data-archive="azerothcore-backup-manual-2026-05-29T14-03-10.tar.gz"' in listing.text
    assert "2026-05-29 14:03 UTC" in listing.text
    assert "Manual" in listing.text
    assert "Backups are automatically deleted after 7 days" in listing.text
    assert "configuration secrets" in listing.text


def _write_archive(path, *, skipped=()):
    with tarfile.open(path, "w:gz") as tf:
        manifest = json.dumps({
            "format_version": 1,
            "databases": ["acore_auth", "acore_characters", "acore_world", "acore_playerbots"],
            "skipped_databases": list(skipped),
        }).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tf.addfile(info, io.BytesIO(manifest))
        for db in ("acore_auth", "acore_characters", "acore_world", "acore_playerbots"):
            dump = b"-- Dump completed on 2026-07-11  3:00:01\n"
            sql = tarfile.TarInfo(f"sql/{db}.sql")
            sql.size = len(dump)
            tf.addfile(sql, io.BytesIO(dump))


def test_backups_list_makes_every_matching_archive_selectable(client, tmp_path):
    backups = tmp_path / "backups"
    _write_archive(backups / "azerothcore-backup-manual-complete.tar.gz")
    _write_archive(backups / "azerothcore-backup-manual-partial.tar.gz", skipped=("acore_world",))
    (backups / "azerothcore-backup-manual-corrupt.tar.gz").write_bytes(b"not gzip")

    response = client.get("/api/backups/list")

    assert response.text.count('class="backup-row backup-available"') == 3
    assert response.text.count('role="button"') == 3
    assert "data-restorable" not in response.text
    assert response.text.count("Available") == 3
    assert response.text.count("/api/backups/download/") == 3
    download = client.get("/api/backups/download/azerothcore-backup-manual-partial.tar.gz")
    assert download.status_code == 200
    assert download.content


def test_backups_metadata_errors_are_safe_in_both_fragments(client, tmp_path):
    archive = tmp_path / "backups" / "azerothcore-backup-manual-secret.tar.gz"
    archive.symlink_to(tmp_path / "secret" / "missing.tar.gz")

    for endpoint in ("/api/backups/summary", "/api/backups/list"):
        response = client.get(endpoint)
        assert response.status_code == 200
        assert "Could not read backup metadata." in response.text
        assert "/secret/path" not in response.text


def test_backups_enumeration_errors_are_safe_in_both_fragments(client, tmp_path):
    backups = tmp_path / "backups"
    backups.rmdir()
    backups.write_text("not a directory")

    for endpoint in ("/api/backups/summary", "/api/backups/list"):
        response = client.get(endpoint)
        assert response.status_code == 200
        assert "Could not read backup metadata." in response.text
        assert "/secret/path" not in response.text


def test_restore_rejects_bad_filename(client):
    r = client.post("/api/action/restore", json={"archive": "../etc/passwd"})
    assert r.status_code == 400


def test_restore_rejects_missing_archive_field(client):
    r = client.post("/api/action/restore", json={})
    assert r.status_code == 422


def test_backup_download_rejects_symlink_outside_backup_directory(client, tmp_path):
    outside = tmp_path / "outside.tar.gz"
    outside.write_bytes(b"database-secret")
    link = tmp_path / "backups" / "azerothcore-backup-manual-link.tar.gz"
    link.symlink_to(outside)

    response = client.get(f"/api/backups/download/{link.name}")

    assert response.status_code == 404
    assert b"database-secret" not in response.content
