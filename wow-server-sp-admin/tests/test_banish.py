from pathlib import Path
from datetime import datetime, timezone

import pytest

from app.services import banish

COLUMNS = (Path(__file__).parent / "data" / "spell_dbc_columns.txt").read_text().split()


def test_definition_matches_spec_values():
    r = banish.SPELL_DBC_ROW
    assert list(r)[0] == "ID" and r["ID"] == 35182
    assert r["Mechanic"] == 0 and r["DurationIndex"] == 0 and r["CumulativeAura"] == 0
    assert r["Attributes"] == 0x24000000 == 603979776
    assert r["AttributesEx"] == 0x80 and r["AttributesEx2"] == 0x04000004 == 67108868
    assert r["AttributesEx3"] == 0x40000 and r["AttributesEx5"] == 8
    assert r["AttributesEx4"] == 0 and r["AttributesEx6"] == 0 and r["AttributesEx7"] == 0
    assert r["CastingTimeIndex"] == 1 and r["RangeIndex"] == 6 and r["ProcChance"] == 101
    assert r["PowerType"] == 0 and r["ManaCost"] == 0 and r["EquippedItemClass"] == -1
    assert (r["Effect_1"], r["Effect_2"], r["Effect_3"]) == (1, 0, 0)
    assert r["EffectDieSides_1"] == 1 and r["EffectBasePoints_1"] == 0
    assert (r["ImplicitTargetA_1"], r["ImplicitTargetA_2"], r["ImplicitTargetA_3"]) == (6, 0, 0)
    assert (r["EffectAura_1"], r["EffectAura_2"], r["EffectAura_3"]) == (0, 0, 0)
    assert r["EffectMiscValue_2"] == 0 and r["EffectMultipleValue_1"] == 1.0
    assert r["EffectChainAmplitude_1"] == r["EffectChainAmplitude_2"] == r["EffectChainAmplitude_3"] == 1.0
    assert r["SpellIconID"] == 96 and r["Name_Lang_enUS"] == "Banish" and r["Name_Lang_Mask"] == 16712190
    assert r["NameSubtext_Lang_Mask"] == 16712172 and r["Description_Lang_Mask"] == 16712190
    assert r["AuraDescription_Lang_Mask"] == 16712190
    assert "boss killer" in r["Description_Lang_enUS"]
    assert (r["StartRecoveryCategory"], r["StartRecoveryTime"]) == (133, 1500)
    assert (r["DefenseType"], r["PreventionType"], r["SchoolMask"]) == (0, 0, 2)
    assert r["SpellVisualID_1"] == 0 and r["SpellVisualID_2"] == 0


def test_definition_columns_exist_in_spell_dbc():
    assert len(COLUMNS) == 234 and COLUMNS[0] == "ID"
    assert set(banish.SPELL_DBC_ROW) <= set(COLUMNS)


def test_definition_columns_follow_table_order():
    assert list(banish.SPELL_DBC_ROW) == sorted(banish.SPELL_DBC_ROW, key=COLUMNS.index)


def test_conditions_row_matches_spec():
    c = banish.CONDITIONS_ROW
    assert c == {"SourceTypeOrReferenceId": 17, "SourceGroup": 0, "SourceEntry": 35182, "SourceId": 0,
                 "ElseGroup": 0, "ConditionTypeOrReference": 31, "ConditionTarget": 1, "ConditionValue1": 3,
                 "ConditionValue2": 0, "ConditionValue3": 0, "NegativeCondition": 0, "ErrorType": 12,
                 "ErrorTextId": 0, "ScriptName": "", "Comment": c["Comment"]}
    assert "creature" in c["Comment"]
    assert banish.CONDITIONS_KEY_COLUMNS == tuple(list(c)[:10])
    assert banish.CONDITIONS_VALUE_COLUMNS == ("NegativeCondition", "ErrorType", "ErrorTextId", "ScriptName", "Comment")


def test_spell_dbc_insert_sql_is_replace_with_named_columns():
    sql, params = banish.spell_dbc_insert_sql()
    assert sql.startswith("REPLACE INTO acore_world.spell_dbc (`ID`,")
    assert sql.count("%s") == len(banish.SPELL_DBC_ROW) == len(params)
    assert params == tuple(banish.SPELL_DBC_ROW.values())


def test_conditions_insert_sql_updates_all_non_key_columns():
    sql, params = banish.conditions_insert_sql()
    assert sql.startswith("INSERT INTO acore_world.conditions (")
    assert "ON DUPLICATE KEY UPDATE" in sql
    for col in banish.CONDITIONS_VALUE_COLUMNS:
        assert f"`{col}` = VALUES(`{col}`)" in sql
    assert params == tuple(banish.CONDITIONS_ROW.values())


def test_select_sql_shapes():
    assert banish.spell_dbc_select_sql().endswith("FROM acore_world.spell_dbc WHERE `ID` = %s")
    sql, params = banish.conditions_select_sql()
    assert sql.count("%s") == 10 and params == tuple(banish.CONDITIONS_ROW[k] for k in banish.CONDITIONS_KEY_COLUMNS)


def _spell_row(**overrides):
    d = dict(banish.SPELL_DBC_ROW); d.update(overrides); return tuple(d.values())


def _cond_row(**overrides):
    d = {k: banish.CONDITIONS_ROW[k] for k in banish.CONDITIONS_VALUE_COLUMNS}; d.update(overrides); return tuple(d.values())


def test_definition_state():
    assert banish.definition_state(None, None) == "missing"
    assert banish.definition_state(None, _cond_row()) == "missing"
    assert banish.definition_state(_spell_row(), _cond_row()) == "installed"
    assert banish.definition_state(_spell_row(), None) == "outdated"
    assert banish.definition_state(_spell_row(AttributesEx7=8), _cond_row()) == "outdated"
    assert banish.definition_state(_spell_row(), _cond_row(NegativeCondition=1)) == "outdated"
    assert banish.definition_state(_spell_row(EffectChainAmplitude_1=1.0000001), _cond_row()) == "installed"


def test_row_matches_decodes_bytearray_strings():
    row = dict(banish.SPELL_DBC_ROW); row["Name_Lang_enUS"] = bytearray(b"Banish")
    assert banish.row_matches(banish.SPELL_DBC_ROW, tuple(row.values()))


def test_parse_docker_time():
    assert banish.parse_docker_time("2026-11-02T10:00:00.123456789Z") == pytest.approx(
        datetime(2026, 11, 2, 10, 0, 0, 123456, tzinfo=timezone.utc).timestamp())
    assert banish.parse_docker_time("2026-11-02T10:00:00Z") == datetime(2026, 11, 2, 10, tzinfo=timezone.utc).timestamp()
    assert banish.parse_docker_time("0001-01-01T00:00:00Z") is None
    assert banish.parse_docker_time(None) is None and banish.parse_docker_time("garbage") is None


def test_worldserver_state_maps_docker_statuses():
    t9 = int(datetime(2026, 11, 2, 9, tzinfo=timezone.utc).timestamp())
    assert banish.worldserver_state("running", "2026-11-02T10:00:00.5Z", t9) == "active"
    assert banish.worldserver_state("running", "2026-11-02T08:00:00Z", t9) == "restart_required"
    assert banish.worldserver_state("running", "2026-11-02T10:00:00Z", None) == "unknown"
    assert banish.worldserver_state("running", None, t9) == "unknown"
    assert banish.worldserver_state("unknown", None, t9) == "unknown"
    for st in ("exited", "missing", "paused", "restarting"):
        assert banish.worldserver_state(st, "2026-11-02T10:00:00Z", t9) == "not_running"


def test_teach_warning():
    assert banish.teach_warning("active") is None
    for st in ("restart_required", "unknown", "not_running"):
        assert "restart" in banish.teach_warning(st).lower()


def test_state_store_roundtrip_and_unreadable(tmp_path):
    store = banish.BanishStateStore(tmp_path / "data")
    assert store.load() is None
    store.save(1793610000)
    assert store.load() == 1793610000
    assert (tmp_path / "data" / "banish.json").exists() and not list((tmp_path / "data").glob("*.tmp"))
    (tmp_path / "data" / "banish.json").write_text("{not json")
    assert store.load() is None


def test_state_store_rejects_non_integer_stamps_and_builds_without_touching_disk(tmp_path):
    store = banish.BanishStateStore(tmp_path / "data")
    assert not (tmp_path / "data").exists()
    store.save(7)
    for bad in ('{"version": 1}', '{"installed_at": "7"}', '{"installed_at": true}', "[7]"):
        (tmp_path / "data" / "banish.json").write_text(bad)
        assert store.load() is None
