import asyncio
import io
import json
import tarfile
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from app.services.actions import ActionResult
from app.services.runner import ActionRunner


def _archive_bytes() -> bytes:
    out = io.BytesIO()
    payload = json.dumps({
        "format_version": 1,
        "databases": ["acore_auth", "acore_characters", "acore_world", "acore_playerbots"],
        "skipped_databases": [],
    }).encode()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
        for db in ("acore_auth", "acore_characters", "acore_world", "acore_playerbots"):
            dump = b"-- Dump completed on 2026-07-11  3:00:01\n"
            sql = tarfile.TarInfo(f"sql/{db}.sql")
            sql.size = len(dump)
            tf.addfile(sql, io.BytesIO(dump))
    return out.getvalue()


def _v2_archive_bytes(sections, *, manifest=None) -> bytes:
    out = io.BytesIO()
    if manifest is None:
        manifest = json.dumps({
            "format_version": 2,
            "databases": ["acore_auth", "acore_characters", "acore_world", "acore_playerbots"],
            "skipped_databases": [],
            "dump_layout": "single-multi-database",
        }).encode()
    dump = b"".join(
        (
            f"-- Current Database: `{database}`\n"
            f"CREATE DATABASE `{database}`;\n"
            f"USE `{database}`;\n"
        ).encode()
        for database in sections
    ) + b"-- Dump completed on 2026-07-11 3:00:01\n"
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tf.addfile(info, io.BytesIO(manifest))
        info = tarfile.TarInfo("sql/azerothcore.sql")
        info.size = len(dump)
        tf.addfile(info, io.BytesIO(dump))
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


def test_import_restore_same_second_uploads_keep_distinct_archives(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    from app.main import app

    with patch("app.main._kick") as kick:
        kick.return_value = type("R", (), {"id": "restore-id"})()
        client = TestClient(app)
        first = client.post("/api/action/import-restore", files={"file": ("first.tar.gz", _archive_bytes())})
        second = client.post("/api/action/import-restore", files={"file": ("second.tar.gz", _archive_bytes())})

    assert first.status_code == second.status_code == 200
    archives = list((tmp_path / "backups").glob("*.tar.gz"))
    assert len(archives) == 2
    assert len({archive.name for archive in archives}) == 2


def test_import_restore_cleans_up_when_dispatch_is_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    import app.main as main

    runner = ActionRunner()
    monkeypatch.setattr(main, "runner", runner)
    action_started = threading.Event()
    release_action = threading.Event()
    thread_errors: list[BaseException] = []

    def hold_action(_on_progress):
        action_started.set()
        release_action.wait()
        return ActionResult.OK

    def run_holding_action():
        loop = asyncio.new_event_loop()
        try:
            async def start_and_wait():
                record = runner.start("hold", hold_action)
                await record.wait()

            loop.run_until_complete(start_and_wait())
        except BaseException as e:  # propagate runner-thread setup failures
            thread_errors.append(e)
        finally:
            loop.close()

    thread = threading.Thread(target=run_holding_action)
    thread.start()
    try:
        assert action_started.wait(timeout=1)
        response = TestClient(main.app).post(
            "/api/action/import-restore",
            files={"file": ("backup.tar.gz", _archive_bytes(), "application/gzip")},
        )
        assert response.status_code == 409
        assert not list((tmp_path / "backups").glob("*.tar.gz"))
    finally:
        release_action.set()
        thread.join(timeout=1)
        assert not thread.is_alive()

    if thread_errors:
        raise thread_errors[0]


def test_import_restore_rejects_invalid_upload_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    from app.main import app

    client = TestClient(app)
    bad_name = client.post("/api/action/import-restore", files={"file": ("bad.zip", b"x")})
    assert bad_name.status_code == 400
    bad_archive = client.post("/api/action/import-restore", files={"file": ("bad.tar.gz", b"not gzip")})
    assert bad_archive.status_code == 400
    assert not list((tmp_path / "backups").glob("*"))


def test_import_restore_rejects_partial_upload_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    payload = json.dumps({
        "format_version": 1,
        "databases": ["acore_auth"],
        "skipped_databases": [],
    }).encode()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))

    from app.main import app
    response = TestClient(app).post(
        "/api/action/import-restore",
        files={"file": ("partial.tar.gz", out.getvalue(), "application/gzip")},
    )

    assert response.status_code == 400
    assert not list((tmp_path / "backups").glob("*"))


def test_import_restore_rejects_noncanonical_v2_stream_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    expected = ("acore_auth", "acore_characters", "acore_world", "acore_playerbots")
    from app.main import app
    client = TestClient(app)

    for sections in (
        expected[:-1],
        (*expected, "unexpected_schema"),
        (expected[1], expected[0], *expected[2:]),
    ):
        response = client.post(
            "/api/action/import-restore",
            files={"file": ("backup.tar.gz", _v2_archive_bytes(sections), "application/gzip")},
        )

        assert response.status_code == 400
        assert "database sections" in response.json()["detail"]
    assert not list((tmp_path / "backups").glob("*"))


def test_import_restore_rejects_duplicate_manifest_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    from app.main import app

    response = TestClient(app).post(
        "/api/action/import-restore",
        files={
            "file": (
                "backup.tar.gz",
                _v2_archive_bytes(
                    ("acore_auth", "acore_characters", "acore_world", "acore_playerbots"),
                    manifest=(
                        b'{"format_version":99,"format_version":2,'
                        b'"databases":["acore_auth","acore_characters","acore_world","acore_playerbots"],'
                        b'"skipped_databases":[],"dump_layout":"single-multi-database"}'
                    ),
                ),
                "application/gzip",
            ),
        },
    )

    assert response.status_code == 400
    assert "archive manifest is malformed" in response.json()["detail"]
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


@pytest.mark.asyncio
async def test_await_thread_completion_waits_for_worker_after_cancellation():
    import app.main as main
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def worker():
        started.set()
        release.wait()
        finished.set()

    request = asyncio.create_task(main._await_thread_completion(worker))
    assert await asyncio.to_thread(started.wait, 1)
    cancelled = False
    try:
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        assert not finished.is_set()
    finally:
        release.set()
        try:
            await request
        except asyncio.CancelledError:
            cancelled = True
    assert cancelled is True
    assert finished.is_set()


def test_import_restore_cleans_hidden_upload_when_validator_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    import app.main as main
    with patch("app.services.actions.validate_canonical_backup", side_effect=RuntimeError("validator crash")):
        response = TestClient(main.app, raise_server_exceptions=False).post(
            "/api/action/import-restore",
            files={"file": ("backup.tar.gz", _archive_bytes(), "application/gzip")},
        )
    assert response.status_code == 500
    assert not list((tmp_path / "backups").glob(".*.upload"))


@pytest.mark.asyncio
async def test_import_copy_does_not_block_health_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    import app.main as main
    from fastapi import HTTPException, UploadFile
    started = threading.Event()
    release = threading.Event()

    def slow_copy(_source, _staged, _limit):
        started.set()
        release.wait()
        return 1

    upload = UploadFile(
        file=io.BytesIO(_archive_bytes()),
        filename="backup.tar.gz",
        size=len(_archive_bytes()),
    )
    with patch.object(main, "_copy_upload_to_staging", side_effect=slow_copy), patch(
        "app.services.actions.validate_canonical_backup", return_value="test rejection",
    ):
        request = asyncio.create_task(main.post_import_restore(upload))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            assert await asyncio.wait_for(main.healthz(), timeout=0.1) == {"status": "ok"}
        finally:
            release.set()
        with pytest.raises(HTTPException) as exc_info:
            await request
        assert "invalid restore archive" in str(exc_info.value.detail)
