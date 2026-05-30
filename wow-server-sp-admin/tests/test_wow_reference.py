from app.services import wow_reference as wr


def test_known_class_names():
    assert wr.class_name(1) == "Warrior"
    assert wr.class_name(6) == "Death Knight"
    assert wr.class_name(11) == "Druid"


def test_known_race_names():
    assert wr.race_name(1) == "Human"
    assert wr.race_name(2) == "Orc"
    assert wr.race_name(11) == "Draenei"


def test_unknown_ids_fall_back_to_numeric():
    assert wr.class_name(99) == "Class 99"
    assert wr.race_name(99) == "Race 99"
    assert wr.zone_name(99999) == "Zone 99999"


def test_faction_mapping_all_playable_races():
    alliance = {1, 3, 4, 7, 11}   # Human, Dwarf, Night Elf, Gnome, Draenei
    horde = {2, 5, 6, 8, 10}      # Orc, Undead, Tauren, Troll, Blood Elf
    for r in alliance:
        assert wr.faction(r) == "Alliance", r
    for r in horde:
        assert wr.faction(r) == "Horde", r


def test_faction_unknown_race():
    assert wr.faction(99) == "Unknown"


def test_some_common_zone_names_present():
    # Elwynn Forest (12), Orgrimmar (1637), Stormwind City (1519).
    assert wr.zone_name(12) == "Elwynn Forest"
    assert wr.zone_name(1637) == "Orgrimmar"
    assert wr.zone_name(1519) == "Stormwind City"
