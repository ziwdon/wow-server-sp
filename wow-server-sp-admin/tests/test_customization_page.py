import re

from fastapi.testclient import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    (tmp_path / "backups").mkdir()
    (tmp_path / "logs").mkdir()
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in [
        "worldserver.conf.dist",
        "playerbots.conf.dist",
        "mod_ahbot.conf.dist",
        "individualProgression.conf.dist",
    ]:
        (dist / name).write_text("")
    from app.state import init_state

    init_state(
        dist_dir=dist,
        admin_yml=tmp_path / "docker-compose.admin.yml",
        override_yml=tmp_path / "docker-compose.override.yml",
        configs_dir=tmp_path / "configs",
        snapshots_dir=tmp_path / "snap",
    )
    from app.main import app

    return TestClient(app)


def _save(client, name="", logo=None, remove_logo=False):
    data = {"name": name}
    if remove_logo:
        data["remove_logo"] = "1"
    files = {"logo": logo if logo is not None else ("", b"", "application/octet-stream")}
    return client.post("/api/customization", data=data, files=files, follow_redirects=False)


def _brand(html: str) -> str:
    return re.search(r'<a class="nav-brand"[^>]*>(.*?)</a>', html, re.S).group(1)


def test_customization_nav_link_comes_after_backups(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    body = client.get("/").text

    assert body.index('href="/backups"') < body.index('href="/customization"')


def test_customization_page_marks_its_nav_link_active(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    body = client.get("/customization").text

    assert re.search(r'class="nav-link active" href="/customization"', body)


def test_default_header_keeps_sword_and_default_name(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    body = client.get("/players").text

    assert "⚔" in _brand(body)
    assert "AzerothCore Admin" in _brand(body)
    assert "<title>AzerothCore Admin · Players</title>" in body
    assert 'rel="icon"' not in body


def test_saved_name_and_logo_show_on_every_page(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = _save(client, name="My Realm", logo=("w.png", PNG, "image/png"))

    assert resp.status_code == 303
    assert resp.headers["location"] == "/customization?saved=1"
    for path, page in [("/", None), ("/stats", "Stats"), ("/backups", "Backups")]:
        body = client.get(path).text
        brand = _brand(body)
        assert "My Realm" in brand
        assert "⚔" not in brand
        logo_src = re.search(r'<img[^>]*src="(/customization/logo\?v=[0-9a-f]{12})"', brand).group(1)
        assert f'<link rel="icon" href="{logo_src}">' in body
        title = "My Realm" if page is None else f"My Realm · {page}"
        assert f"<title>{title}</title>" in body


def test_logo_route_serves_the_uploaded_bytes(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _save(client, logo=("w.jpg", JPEG, "image/jpeg"))

    resp = client.get("/customization/logo")

    assert resp.status_code == 200
    assert resp.content == JPEG
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_logo_route_is_404_without_a_logo(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    assert client.get("/customization/logo").status_code == 404


def test_empty_file_field_keeps_the_existing_logo(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _save(client, logo=("w.png", PNG, "image/png"))

    _save(client, name="Renamed")

    assert client.get("/customization/logo").content == PNG
    assert "Renamed" in _brand(client.get("/").text)


def test_remove_logo_brings_back_the_sword(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _save(client, logo=("w.png", PNG, "image/png"))

    _save(client, remove_logo=True)

    assert client.get("/customization/logo").status_code == 404
    assert "⚔" in _brand(client.get("/").text)


def test_invalid_upload_redirects_with_error_and_changes_nothing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _save(client, name="Kept")

    resp = _save(client, name="New", logo=("x.svg", b"<svg></svg>", "image/svg+xml"))

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/customization?error=")
    page = client.get(resp.headers["location"]).text
    assert re.search(r'class="banner error"[^>]*>The logo must be a PNG, JPG, WebP or GIF image\.<', page)
    assert "Kept" in _brand(page)


def test_customization_form_shows_the_saved_name(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _save(client, name="My Realm")

    body = client.get("/customization").text

    assert re.search(r'<input[^>]*name="name"[^>]*value="My Realm"', body)
    assert 'enctype="multipart/form-data"' in body


def test_name_is_html_escaped_in_the_header(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _save(client, name="<b>Realm</b>")

    body = client.get("/").text

    assert "<b>Realm</b>" not in body
    assert "&lt;b&gt;Realm&lt;/b&gt;" in _brand(body)
