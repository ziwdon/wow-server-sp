from app.services.resolver import EffectiveValue, resolve_effective


def test_dist_default_wins_when_nothing_else_set():
    r = resolve_effective(
        key="Foo.Enable",
        env_var="AC_FOO_ENABLE",
        dist_default="1",
        conf_value=None,
        override_env={},
        admin_env={},
    )
    assert r.value == "1"
    assert r.source == "dist"


def test_conf_overrides_dist():
    r = resolve_effective(
        key="Foo.Enable",
        env_var="AC_FOO_ENABLE",
        dist_default="1",
        conf_value="0",
        override_env={},
        admin_env={},
    )
    assert r.value == "0"
    assert r.source == "conf"


def test_override_beats_conf():
    r = resolve_effective(
        key="Foo.Enable",
        env_var="AC_FOO_ENABLE",
        dist_default="1",
        conf_value="0",
        override_env={"AC_FOO_ENABLE": "2"},
        admin_env={},
    )
    assert r.value == "2"
    assert r.source == "installer"


def test_admin_beats_everything():
    r = resolve_effective(
        key="Foo.Enable",
        env_var="AC_FOO_ENABLE",
        dist_default="1",
        conf_value="0",
        override_env={"AC_FOO_ENABLE": "2"},
        admin_env={"AC_FOO_ENABLE": "3"},
    )
    assert r.value == "3"
    assert r.source == "admin"
