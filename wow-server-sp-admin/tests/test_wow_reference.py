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


def test_newly_mapped_accessible_zones():
    # Zones confirmed reachable by bots or otherwise present in the DBC
    # as normal in-world areas that were missing from the original mapping.
    assert wr.zone_name(25)   == "Blackrock Mountain"
    assert wr.zone_name(616)  == "Hyjal"
    assert wr.zone_name(1941) == "Caverns of Time"
    assert wr.zone_name(2257) == "Deeprun Tram"
    assert wr.zone_name(3455) == "The North Sea"
    assert wr.zone_name(3535) == "Hellfire Citadel"
    assert wr.zone_name(3605) == "Hyjal Past"
    assert wr.zone_name(3917) == "Auchindoun"
    assert wr.zone_name(4742) == "Hrothgar's Landing"
    assert wr.zone_name(4896) == "The Frozen Throne"


def test_newly_mapped_inaccessible_zones():
    # Dev/test/unused zones from the DBC — should display with (inaccessible)
    # so admins know where a bot ended up rather than seeing a bare zone ID.
    assert wr.zone_name(22)   == "Programmer Isle (inaccessible)"
    assert wr.zone_name(876)  == "GM Island (inaccessible)"
    assert wr.zone_name(3540) == "Twisting Nether (inaccessible)"
    assert wr.zone_name(3817) == "Testing (inaccessible)"


_DAY = 86400


def test_relative_last_online_online_char():
    # An online character shows "online" regardless of its stored logout_time.
    assert wr.relative_last_online(1000, True, now=10 * _DAY) == "online"


def test_relative_last_online_never_logged_in():
    # logout_time == 0 means the character has never logged in.
    assert wr.relative_last_online(0, False, now=10 * _DAY) == "never"


def test_relative_last_online_today():
    now = 10 * _DAY + 3600
    assert wr.relative_last_online(10 * _DAY, False, now=now) == "today"


def test_relative_last_online_yesterday():
    now = 10 * _DAY + 3600
    assert wr.relative_last_online(now - _DAY, False, now=now) == "yesterday"


def test_relative_last_online_previous_calendar_day_within_24_hours():
    # Sariel/Loriel case: previous calendar day, but less than 24 hours ago.
    now = 1781789871       # 2026-06-18 13:37:51 UTC
    logout = 1781711624    # 2026-06-17 15:53:44 UTC
    assert wr.relative_last_online(logout, False, now=now) == "yesterday"


def test_relative_last_online_multiple_days():
    now = 10 * _DAY
    assert wr.relative_last_online(now - 5 * _DAY, False, now=now) == "5 days ago"


def test_relative_last_online_future_logout_clamps_to_today():
    # Clock skew: a logout_time slightly ahead of now must not go negative.
    now = 10 * _DAY
    assert wr.relative_last_online(now + 100, False, now=now) == "today"
