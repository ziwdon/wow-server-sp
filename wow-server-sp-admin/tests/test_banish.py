from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from mysql.connector.errors import DatabaseError, ProgrammingError

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


# --- database operations ---


def _conn(mock_connect):
    cur = MagicMock()
    conn = mock_connect.return_value
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def _executed(cur):
    return " ".join(str(c.args[0]) for c in cur.execute.call_args_list)


@patch("app.services.banish.mysql.connector.connect")
def test_collect_characters_includes_knows_and_excludes_bots(mock_connect):
    conn, cur = _conn(mock_connect)
    cur.fetchall.return_value = [(101, "CARLOS", "Sariel", 60, 0, 1), (102, "CARLOS", "Vegivaca", 70, 1, 0)]
    rows = banish.collect_characters(host="h", port=3306, user="u", password="p")
    assert rows[0].knows is True and rows[1].online is True and rows[1].knows is False
    sql = _executed(cur)
    assert "a.username NOT LIKE 'RNDBOT%%'" in sql and "a.username <> 'ahbot'" in sql
    assert "character_spell" in sql and "35182" in sql


@patch("app.services.banish.mysql.connector.connect")
def test_fetch_definition_raises_unsupported_when_table_missing(mock_connect):
    conn, cur = _conn(mock_connect)
    cur.execute.side_effect = ProgrammingError(msg="Table 'acore_world.spell_dbc' doesn't exist", errno=1146)
    with pytest.raises(banish.SpellDbcUnsupported):
        banish.fetch_definition(cur)


@patch("app.services.banish.mysql.connector.connect")
def test_collect_status_installed_and_active(mock_connect, tmp_path):
    conn, cur = _conn(mock_connect)
    cur.fetchone.side_effect = [_spell_row(), _cond_row()]
    store = banish.BanishStateStore(tmp_path); store.save(1793610000)   # 2026-11-02T09:00:00Z
    info = MagicMock(status="running", started_at="2026-11-02T10:00:00.000000001Z")
    st = banish.collect_status(store=store, inspect=lambda: info, host="h", port=3306, user="u", password="p")
    assert (st.support, st.definition, st.worldserver, st.installed_at, st.version) == ("ok", "installed", "active", 1793610000, 1)


@patch("app.services.banish.mysql.connector.connect")
def test_collect_status_reports_missing_table_and_db_errors(mock_connect, tmp_path):
    conn, cur = _conn(mock_connect)
    cur.execute.side_effect = ProgrammingError(msg="no table", errno=1146)
    st = banish.collect_status(store=banish.BanishStateStore(tmp_path), inspect=lambda: MagicMock(status="running", started_at=None), host="h", port=3306, user="u", password="p")
    assert st.support == "no_spell_dbc_table" and st.definition == "missing"
    mock_connect.side_effect = DatabaseError(msg="boom", errno=2003)
    st = banish.collect_status(store=banish.BanishStateStore(tmp_path), inspect=lambda: MagicMock(status="running", started_at=None), host="h", port=3306, user="u", password="p")
    assert st.support == "db_error" and "boom" in (st.message or "")


@patch("app.services.banish.mysql.connector.connect")
def test_collect_status_survives_a_failing_docker_inspect(mock_connect, tmp_path):
    conn, cur = _conn(mock_connect)
    cur.fetchone.side_effect = [_spell_row(), _cond_row()]
    store = banish.BanishStateStore(tmp_path); store.save(1793610000)

    def no_socket():
        raise RuntimeError("Error while fetching server API version")

    st = banish.collect_status(store=store, inspect=no_socket, host="h", port=3306, user="u", password="p")
    assert (st.support, st.definition, st.worldserver) == ("ok", "installed", "unknown")


@patch("app.services.banish.mysql.connector.connect")
def test_install_writes_rows_and_stamps_state(mock_connect, tmp_path):
    conn, cur = _conn(mock_connect)
    cur.fetchone.side_effect = [None, None]
    store = banish.BanishStateStore(tmp_path)
    res = banish.install_definition(store=store, now=1793610000, host="h", port=3306, user="u", password="p")
    sql = _executed(cur)
    assert "REPLACE INTO acore_world.spell_dbc" in sql and "INSERT INTO acore_world.conditions" in sql
    conn.commit.assert_called_once()
    assert res.status == "installed" and "restart" in res.message.lower()
    assert store.load() == 1793610000


@patch("app.services.banish.mysql.connector.connect")
def test_install_unchanged_does_not_restamp(mock_connect, tmp_path):
    conn, cur = _conn(mock_connect)
    cur.fetchone.side_effect = [_spell_row(), _cond_row()]
    store = banish.BanishStateStore(tmp_path); store.save(1793610000)
    res = banish.install_definition(store=store, now=1793700000, host="h", port=3306, user="u", password="p")
    assert "REPLACE INTO" not in _executed(cur) and res.status == "unchanged"
    assert store.load() == 1793610000


@patch("app.services.banish.mysql.connector.connect")
def test_install_unchanged_but_missing_state_restamps(mock_connect, tmp_path):
    conn, cur = _conn(mock_connect)
    cur.fetchone.side_effect = [_spell_row(), _cond_row()]
    store = banish.BanishStateStore(tmp_path)
    res = banish.install_definition(store=store, now=1793700000, host="h", port=3306, user="u", password="p")
    assert res.status == "installed" and store.load() == 1793700000 and "REPLACE INTO" not in _executed(cur)


@patch("app.services.banish.mysql.connector.connect")
def test_install_rolls_back_and_reraises_on_error(mock_connect, tmp_path):
    conn, cur = _conn(mock_connect)
    cur.fetchone.side_effect = [None, None]
    def boom(sql, *a):
        if sql.startswith("INSERT INTO acore_world.conditions"):
            raise DatabaseError(msg="disk full", errno=1114)
    cur.execute.side_effect = boom
    store = banish.BanishStateStore(tmp_path)
    with pytest.raises(DatabaseError):
        banish.install_definition(store=store, now=1, host="h", port=3306, user="u", password="p")
    conn.rollback.assert_called_once(); conn.commit.assert_not_called()
    assert store.load() is None


@patch("app.services.banish.mysql.connector.connect")
def test_install_reports_success_when_the_state_file_cannot_be_written(mock_connect):
    conn, cur = _conn(mock_connect)
    cur.fetchone.side_effect = [None, None]
    store = MagicMock()
    store.load.return_value = None
    store.save.side_effect = OSError("read-only file system")
    res = banish.install_definition(store=store, now=5, host="h", port=3306, user="u", password="p")
    conn.commit.assert_called_once()
    assert res.status == "installed" and "could not record the install time" in res.message


def _teach(cur_rows, store, inspect=None):
    with patch("app.services.banish.mysql.connector.connect") as mock_connect:
        conn, cur = _conn(mock_connect)
        cur.fetchone.side_effect = cur_rows
        res = banish.teach(guid=101, store=store, inspect=inspect or (lambda: MagicMock(status="running", started_at="2026-11-02T10:00:00Z")),
                           host="h", port=3306, user="u", password="p")
        return res, conn, cur


def test_teach_inserts_row_with_spec_mask_255(tmp_path):
    store = banish.BanishStateStore(tmp_path); store.save(1793610000)
    res, conn, cur = _teach([(0, "Sariel"), _spell_row(), _cond_row(), None], store)
    assert res.status == "taught" and "Sariel" in res.message and "Blizzard" in res.message and res.warning is None
    insert = [c for c in cur.execute.call_args_list if "INSERT" in str(c.args[0])][0]
    assert "acore_characters.character_spell" in insert.args[0] and insert.args[1] == (101, 35182, 255)
    conn.commit.assert_called_once()


def test_teach_warns_when_restart_required(tmp_path):
    store = banish.BanishStateStore(tmp_path); store.save(1793610000)
    res, *_ = _teach([(0, "Sariel"), _spell_row(), _cond_row(), None], store,
                     inspect=lambda: MagicMock(status="running", started_at="2026-11-02T08:00:00Z"))
    assert res.status == "taught" and "restart" in res.warning.lower()


def test_teach_still_succeeds_when_docker_inspect_fails_after_the_commit(tmp_path):
    store = banish.BanishStateStore(tmp_path); store.save(1793610000)

    def no_socket():
        raise RuntimeError("Error while fetching server API version")

    res, conn, cur = _teach([(0, "Sariel"), _spell_row(), _cond_row(), None], store, inspect=no_socket)
    conn.commit.assert_called_once()
    assert res.status == "taught" and "restart" in res.warning.lower()


def test_teach_validations(tmp_path):
    store = banish.BanishStateStore(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        _teach([None], store)
    with pytest.raises(ValueError, match="log out"):
        _teach([(1, "Sariel")], store)
    with pytest.raises(ValueError, match="Install"):
        _teach([(0, "Sariel"), None, None], store)
    res, conn, cur = _teach([(0, "Sariel"), _spell_row(), _cond_row(), (1,)], store)
    assert res.status == "noop" and "already" in res.message and "INSERT" not in _executed(cur)


@patch("app.services.banish.mysql.connector.connect")
def test_forget_deletes_row_or_noops(mock_connect):
    conn, cur = _conn(mock_connect)
    cur.fetchone.side_effect = [(0, "Sariel")]; cur.rowcount = 1
    res = banish.forget(guid=101, host="h", port=3306, user="u", password="p")
    assert res.status == "forgotten" and "Sariel" in res.message
    delete = [c for c in cur.execute.call_args_list if "DELETE" in str(c.args[0])][0]
    assert "acore_characters.character_spell" in delete.args[0] and delete.args[1] == (101, 35182)
    cur.execute.reset_mock(); cur.fetchone.side_effect = [(0, "Sariel")]; cur.rowcount = 0
    assert banish.forget(guid=101, host="h", port=3306, user="u", password="p").status == "noop"
    cur.fetchone.side_effect = [(1, "Sariel")]
    with pytest.raises(ValueError, match="log out"):
        banish.forget(guid=101, host="h", port=3306, user="u", password="p")
