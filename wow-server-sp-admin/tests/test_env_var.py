from pathlib import Path

from app.services.env_var import config_key_to_ac_env_var

GOLDEN = Path(__file__).parent / "data" / "env_var_golden.txt"


def test_golden_file_matches_bash_helper():
    failures = []
    for line in GOLDEN.read_text().splitlines():
        key, expected = line.split("\t")
        actual = config_key_to_ac_env_var(key)
        if actual != expected:
            failures.append(f"{key}: expected {expected}, got {actual}")
    assert not failures, "\n".join(failures)


def test_known_examples_from_claude_md():
    assert config_key_to_ac_env_var("AiPlayerbot.Enabled") == "AC_AI_PLAYERBOT_ENABLED"
    assert (
        config_key_to_ac_env_var("Respawn.DynamicRateGameObject")
        == "AC_RESPAWN_DYNAMIC_RATE_GAME_OBJECT"
    )
    assert config_key_to_ac_env_var("SkillGain.Crafting") == "AC_SKILL_GAIN_CRAFTING"


def test_digits_introduce_word_boundaries():
    # letter→digit and digit→letter both insert underscores
    assert config_key_to_ac_env_var("Some123Key") == "AC_SOME_123_KEY"
    assert config_key_to_ac_env_var("X1Y2") == "AC_X_1_Y_2"


def test_spaces_and_hyphens_become_underscores():
    assert config_key_to_ac_env_var("foo bar-baz.qux") == "AC_FOO_BAR_BAZ_QUX"
