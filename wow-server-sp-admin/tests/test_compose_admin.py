from app.services.compose_admin import AdminCompose, validate_restored_overlay


def test_read_empty_file_returns_empty_env(tmp_path):
    path = tmp_path / "admin.yml"
    path.write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    c = AdminCompose(path, snapshots_dir=snaps)
    assert c.read_env() == {}


def test_write_then_read_roundtrip(tmp_path):
    path = tmp_path / "admin.yml"
    path.write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    c = AdminCompose(path, snapshots_dir=snaps)
    c.write_env({"AC_FOO": "1", "AC_BAR": "abc"})
    assert c.read_env() == {"AC_FOO": "1", "AC_BAR": "abc"}


def test_write_is_in_place_no_tmp_or_rename(tmp_path):
    """admin.yml is bind-mounted in production; rename(2) over a bind-mount
    fails with EBUSY. Write must be in place (open + truncate + write),
    not tmp+rename. Verify no .tmp leftover and that the inode is stable."""
    path = tmp_path / "admin.yml"
    path.write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    inode_before = path.stat().st_ino
    c = AdminCompose(path, snapshots_dir=snaps)
    c.write_env({"AC_X": "y"})
    assert not (tmp_path / "admin.yml.tmp").exists()
    assert path.stat().st_ino == inode_before


def test_snapshot_lands_in_snapshots_dir_not_next_to_admin_yml(tmp_path):
    """Snapshots MUST go to snapshots_dir, not admin.yml's parent. In
    production admin.yml's parent (/ac/) is a read-only bind mount, so
    a sibling-file snapshot would hit EROFS."""
    path = tmp_path / "admin.yml"
    path.write_text("services:\n  ac-worldserver:\n    environment:\n      AC_OLD: '1'\n")
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    c = AdminCompose(path, snapshots_dir=snaps)
    backup = c.snapshot()
    assert backup.exists()
    assert backup.parent == snaps
    assert backup.name.startswith("admin.yml.bak.")
    assert "AC_OLD" in backup.read_text()
    # Confirm no snapshot leaked next to the live file.
    assert list(tmp_path.glob("admin.yml.bak.*")) == []


def test_write_preserves_other_services_and_keys(tmp_path):
    """The admin must only edit ac-worldserver.environment; everything
    else in the file (other services, top-level keys, extra fields on
    ac-worldserver) must round-trip untouched."""
    path = tmp_path / "admin.yml"
    path.write_text(
        "services:\n"
        "  ac-worldserver:\n"
        "    environment:\n"
        "      AC_OLD: '1'\n"
        "    deploy:\n"
        "      resources:\n"
        "        limits:\n"
        "          memory: 4G\n"
        "  ac-database:\n"
        "    environment:\n"
        "      MYSQL_X: 'y'\n"
        "x-extras:\n"
        "  note: hand-written\n"
    )
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    c = AdminCompose(path, snapshots_dir=snaps)
    c.write_env({"AC_FOO": "1"})
    out = path.read_text()
    assert "AC_FOO" in out
    assert "ac-database" in out
    assert "MYSQL_X" in out
    assert "x-extras" in out
    assert "memory: 4G" in out


def test_validate_restored_overlay_accepts_only_the_managed_environment_shape(tmp_path):
    path = tmp_path / "admin.yml"
    path.write_text(
        "services:\n"
        "  ac-worldserver:\n"
        "    environment:\n"
        "      AC_FOO_ENABLE: '1'\n"
    )

    assert validate_restored_overlay(path, allowed_env_vars={"AC_FOO_ENABLE"}) is None


def test_validate_restored_overlay_rejects_malformed_extra_and_unapproved_content(tmp_path):
    path = tmp_path / "admin.yml"
    path.write_text("services: [not-a-mapping\n")
    assert "malformed" in validate_restored_overlay(path, allowed_env_vars=set())

    path.write_text("services:\n  ac-database:\n    environment: {}\n")
    assert "extra services" in validate_restored_overlay(path, allowed_env_vars=set())

    path.write_text(
        "services:\n"
        "  ac-worldserver:\n"
        "    environment:\n"
        "      AC_NOT_APPROVED: '1'\n"
    )
    assert "not approved" in validate_restored_overlay(path, allowed_env_vars=set())

    path.write_text(
        "services:\n"
        "  ac-worldserver:\n"
        "    environment:\n"
        "      AC_AUCTION_HOUSE_BOT_GUIDS: '1'\n"
    )
    assert "blocked key" in validate_restored_overlay(
        path,
        allowed_env_vars={"AC_AUCTION_HOUSE_BOT_GUIDS"},
    )
