from pathlib import Path

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
    assert 'hx-post="/api/action/backup"' in r.text
    assert 'id="restore-btn"' in r.text
    assert "Restore selected" in r.text
    assert "/static/backups.js?v=" in r.text


def test_nav_has_backups_link(client):
    r = client.get("/")
    assert 'href="/backups"' in r.text


def test_backups_list_endpoint(client):
    r = client.get("/api/backups/list")
    assert r.status_code == 200


def test_backups_list_renders_rows_and_disclaimer(client, tmp_path):
    archive = tmp_path / "backups" / "azerothcore-backup-manual-2026-05-29T14-03-10.tar.gz"
    archive.write_bytes(b"backup archive")

    r = client.get("/api/backups/list")

    assert r.status_code == 200
    assert 'class="backup-row"' in r.text
    assert 'data-archive="azerothcore-backup-manual-2026-05-29T14-03-10.tar.gz"' in r.text
    assert "2026-05-29 14:03 UTC" in r.text
    assert "Manual" in r.text
    assert "Backups are automatically deleted after 7 days" in r.text
    assert "configuration secrets" in r.text


def test_restore_rejects_bad_filename(client):
    r = client.post("/api/action/restore", json={"archive": "../etc/passwd"})
    assert r.status_code == 400


def test_restore_rejects_missing_archive_field(client):
    r = client.post("/api/action/restore", json={})
    assert r.status_code == 422
