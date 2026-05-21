from pathlib import Path

from app.services.config_index import KeyEntry, parse_dist_file


def test_parses_three_keys_with_comments_and_defaults(tmp_path):
    fixture = Path(__file__).parent / "data" / "sample.conf.dist"
    entries = parse_dist_file(fixture)

    assert [e.key for e in entries] == ["Foo.Enable", "Bar.Count", "Baz.Name"]

    foo = entries[0]
    assert foo.default == "1"
    assert foo.inferred_type == "int"
    assert "Whether the Foo subsystem is enabled." in foo.comment
    assert foo.source_file == fixture.name
    assert foo.line_number == 11
    assert foo.env_var == "AC_FOO_ENABLE"

    bar = entries[1]
    assert bar.default == "10"
    assert bar.inferred_type == "int"

    baz = entries[2]
    assert baz.default == ""
    assert baz.inferred_type == "string"


def test_empty_string_default_is_recognized(tmp_path):
    f = tmp_path / "x.conf.dist"
    f.write_text(
        "#\n"
        "#    K.Test\n"
        "#        Default: empty\n"
        "#\n"
        '\nK.Test = ""\n'
    )
    entries = parse_dist_file(f)
    assert entries[0].default == ""
    assert entries[0].inferred_type == "string"


def test_keys_with_floats_get_float_type(tmp_path):
    f = tmp_path / "x.conf.dist"
    f.write_text("#\n#    K\n#\n\nK = 1.5\n")
    entries = parse_dist_file(f)
    assert entries[0].inferred_type == "float"


def test_zero_and_one_are_ints_not_bools(tmp_path):
    f = tmp_path / "x.conf.dist"
    f.write_text("#\n#    A\n#\n\nA = 0\n#\n#    B\n#\n\nB = 1\n")
    entries = parse_dist_file(f)
    assert [e.inferred_type for e in entries] == ["int", "int"]
