import io
import json
import tarfile
from unittest.mock import patch

from fastapi.testclient import TestClient


def _archive_bytes() -> bytes:
    out = io.BytesIO()
    payload = json.dumps({"format_version": 1, "databases": ["acore_auth"]}).encode()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return out.getvalue()


def test_import_restore_validates_and_dispatches(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    from app.main import app

    with patch("app.main._kick") as kick:
        kick.return_value = type("R", (), {"id": "restore-id"})()
        r = TestClient(app).post(
            "/api/action/import-restore",
            files={"file": ("backup.tar.gz", _archive_bytes(), "application/gzip")},
        )
    assert r.status_code == 200
    assert r.json()["id"] == "restore-id"
    assert len(list((tmp_path / "backups").glob("*.tar.gz"))) == 1


def test_import_restore_rejects_invalid_upload_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    from app.main import app

    client = TestClient(app)
    bad_name = client.post("/api/action/import-restore", files={"file": ("bad.zip", b"x")})
    assert bad_name.status_code == 400
    bad_archive = client.post("/api/action/import-restore", files={"file": ("bad.tar.gz", b"not gzip")})
    assert bad_archive.status_code == 400
    assert not list((tmp_path / "backups").glob("*"))


def test_import_restore_enforces_size_cap_without_leaving_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    import app.main as main

    monkeypatch.setattr(main, "_MAX_IMPORT_BYTES", 4)
    r = TestClient(main.app).post(
        "/api/action/import-restore", files={"file": ("large.tar.gz", b"12345")}
    )
    assert r.status_code == 413
    assert not list((tmp_path / "backups").glob("*"))


def test_import_restore_handles_write_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    from app.main import app

    with patch("pathlib.Path.open", side_effect=OSError("disk full")):
        r = TestClient(app).post(
            "/api/action/import-restore", files={"file": ("backup.tar.gz", b"x")}
        )
    assert r.status_code == 500
