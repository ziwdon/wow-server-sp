import os
from pathlib import Path

from app.state import init_state, list_keys_resolved


def _setup_state(tmp_path: Path, admin_content: str = "services:\n  ac-worldserver:\n    environment: {}\n") -> Path:
    """Create minimal dist files and call init_state. Returns admin_yml path."""
    dist = tmp_path / "dist"
    dist.mkdir()
    # All four dist files must exist; empty content is fine for cache tests.
    for name in [
        "worldserver.conf.dist",
        "playerbots.conf.dist",
        "mod_ahbot.conf.dist",
        "individualProgression.conf.dist",
    ]:
        (dist / name).write_text("")

    override_yml = tmp_path / "docker-compose.override.yml"
    override_yml.write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    admin_yml = tmp_path / "docker-compose.admin.yml"
    admin_yml.write_text(admin_content)
    configs = tmp_path / "configs"
    configs.mkdir()
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()

    init_state(
        dist_dir=dist,
        admin_yml=admin_yml,
        override_yml=override_yml,
        configs_dir=configs,
        snapshots_dir=snapshots,
    )
    return admin_yml


def test_list_keys_resolved_returns_list(tmp_path):
    _setup_state(tmp_path)
    result = list_keys_resolved()
    assert isinstance(result, list)


def test_list_keys_resolved_returns_same_object_on_second_call(tmp_path):
    _setup_state(tmp_path)
    first = list_keys_resolved()
    second = list_keys_resolved()
    assert first is second  # cache hit: same list object


def test_list_keys_resolved_invalidates_when_admin_yml_changes(tmp_path):
    admin_yml = _setup_state(tmp_path)
    first = list_keys_resolved()

    # Advance mtime by 1 second to ensure cache key changes.
    old_mtime = os.path.getmtime(admin_yml)
    admin_yml.write_text(
        "services:\n  ac-worldserver:\n    environment:\n      AC_TEST_KEY: \"1\"\n"
    )
    os.utime(admin_yml, (old_mtime + 1.0, old_mtime + 1.0))

    second = list_keys_resolved()
    assert first is not second  # cache miss: new list object
