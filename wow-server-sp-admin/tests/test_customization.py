import hashlib
import json

import pytest

from app.services.customization import CustomizationError, CustomizationStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64


def _store(tmp_path) -> CustomizationStore:
    return CustomizationStore(tmp_path / "admin-data")


def test_defaults_without_saved_settings(tmp_path):
    branding = _store(tmp_path).branding()

    assert branding.name == "AzerothCore Admin"
    assert branding.custom_name == ""
    assert branding.logo_url is None


def test_saved_name_is_trimmed_and_shown(tmp_path):
    store = _store(tmp_path)

    store.save(name="  My Realm  ", logo=None, remove_logo=False)

    assert store.branding().name == "My Realm"
    assert store.branding().custom_name == "My Realm"


def test_empty_name_falls_back_to_default(tmp_path):
    store = _store(tmp_path)
    store.save(name="My Realm", logo=None, remove_logo=False)

    store.save(name="   ", logo=None, remove_logo=False)

    assert store.branding().name == "AzerothCore Admin"
    assert store.branding().custom_name == ""


def test_name_of_32_characters_is_accepted(tmp_path):
    store = _store(tmp_path)

    store.save(name="x" * 32, logo=None, remove_logo=False)

    assert store.branding().name == "x" * 32


@pytest.mark.parametrize("bad", ["x" * 33, "My\nRealm", "My\x00Realm", "Tab\there"])
def test_too_long_or_control_character_name_is_refused(tmp_path, bad):
    store = _store(tmp_path)
    store.save(name="Kept", logo=None, remove_logo=False)

    with pytest.raises(CustomizationError):
        store.save(name=bad, logo=None, remove_logo=False)

    assert store.branding().name == "Kept"


@pytest.mark.parametrize(
    ("data", "media_type"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")],
)
def test_uploaded_logo_is_served_with_its_detected_type(tmp_path, data, media_type):
    store = _store(tmp_path)

    store.save(name="", logo=data, remove_logo=False)

    assert store.logo() == (data, media_type)
    sha12 = hashlib.sha256(data).hexdigest()[:12]
    assert store.branding().logo_url == f"/customization/logo?v={sha12}"


@pytest.mark.parametrize(
    "data",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"plain text",
        b"RIFF\x24\x00\x00\x00WAVEfmt ",
        b"",
    ],
)
def test_unsupported_logo_is_refused_and_old_logo_kept(tmp_path, data):
    store = _store(tmp_path)
    store.save(name="", logo=PNG, remove_logo=False)

    with pytest.raises(CustomizationError):
        store.save(name="", logo=data, remove_logo=False)

    assert store.logo() == (PNG, "image/png")


def test_logo_over_512_kib_is_refused(tmp_path):
    store = _store(tmp_path)
    too_big = PNG + b"\x00" * (512 * 1024 - len(PNG) + 1)

    with pytest.raises(CustomizationError):
        store.save(name="", logo=too_big, remove_logo=False)

    assert store.logo() is None


def test_logo_of_exactly_512_kib_is_accepted(tmp_path):
    store = _store(tmp_path)
    just_fits = PNG + b"\x00" * (512 * 1024 - len(PNG))

    store.save(name="", logo=just_fits, remove_logo=False)

    assert store.logo() == (just_fits, "image/png")


def test_invalid_name_does_not_store_the_logo_sent_with_it(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(CustomizationError):
        store.save(name="x" * 33, logo=PNG, remove_logo=False)

    assert store.logo() is None
    assert list((tmp_path / "admin-data").rglob("logo.*")) == []


def test_saving_only_a_name_keeps_the_logo(tmp_path):
    store = _store(tmp_path)
    store.save(name="", logo=PNG, remove_logo=False)

    store.save(name="Renamed", logo=None, remove_logo=False)

    assert store.logo() == (PNG, "image/png")
    assert store.branding().name == "Renamed"


def test_replacing_the_logo_removes_the_old_file(tmp_path):
    store = _store(tmp_path)
    store.save(name="", logo=PNG, remove_logo=False)

    store.save(name="", logo=JPEG, remove_logo=False)

    assert store.logo() == (JPEG, "image/jpeg")
    assert [p.name for p in (tmp_path / "admin-data").rglob("logo.*")] == ["logo.jpg"]


def test_remove_logo_deletes_it(tmp_path):
    store = _store(tmp_path)
    store.save(name="Named", logo=PNG, remove_logo=False)

    store.save(name="Named", logo=None, remove_logo=True)

    assert store.logo() is None
    assert store.branding().logo_url is None
    assert list((tmp_path / "admin-data").rglob("logo.*")) == []


def test_corrupt_settings_file_falls_back_to_defaults(tmp_path):
    store = _store(tmp_path)
    store.save(name="Named", logo=None, remove_logo=False)
    settings = next((tmp_path / "admin-data").rglob("*.json"))
    settings.write_text("{not json")

    assert store.branding().name == "AzerothCore Admin"
    assert store.logo() is None


def test_settings_pointing_at_a_missing_logo_show_no_logo(tmp_path):
    store = _store(tmp_path)
    store.save(name="", logo=PNG, remove_logo=False)
    next((tmp_path / "admin-data").rglob("logo.png")).unlink()

    assert store.logo() is None
    assert store.branding().logo_url is None


def test_settings_with_unknown_logo_type_show_no_logo(tmp_path):
    store = _store(tmp_path)
    store.save(name="Named", logo=PNG, remove_logo=False)
    settings = next((tmp_path / "admin-data").rglob("*.json"))
    raw = json.loads(settings.read_text())
    raw["logo_type"] = "../../etc/passwd"
    settings.write_text(json.dumps(raw))

    assert store.logo() is None
    assert store.branding().logo_url is None
