from app.services import progression


def test_expansion_from_progression_boundaries():
    assert progression.expansion_from_state(0) == "vanilla"
    assert progression.expansion_from_state(7) == "vanilla"
    assert progression.expansion_from_state(8) == "tbc"
    assert progression.expansion_from_state(12) == "tbc"
    assert progression.expansion_from_state(13) == "wotlk"
    assert progression.expansion_from_state(18) == "wotlk"


def test_target_state_for_expansion():
    assert progression.target_state_for_expansion("vanilla") == 0
    assert progression.target_state_for_expansion("tbc") == 8
    assert progression.target_state_for_expansion("wotlk") == 13
