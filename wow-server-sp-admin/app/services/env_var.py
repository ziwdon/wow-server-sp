"""Python port of scripts/install-azerothcore.sh:config_key_to_ac_env_var.

Matches AzerothCore Config.cpp::IniKeyToEnvVarKey:
  - prefix `AC_`
  - dots, spaces, hyphens become underscores
  - insert `_` at lowercase→uppercase and letter↔digit boundaries
  - uppercase everything

Verified against the bash helper via tests/data/env_var_golden.txt.
"""


def _is_upper(ch: str) -> bool:
    return ch.isascii() and ch.isalpha() and ch.isupper()


def _is_digit(ch: str) -> bool:
    return ch.isascii() and ch.isdigit()


def config_key_to_ac_env_var(key: str) -> str:
    out = ["AC_"]
    for i, curr in enumerate(key):
        if curr in (" ", ".", "-"):
            out.append("_")
            continue

        if i < len(key) - 1:
            nxt = key[i + 1]
            if not _is_upper(curr) and _is_upper(nxt):
                out.append(curr.upper())
                out.append("_")
                continue
            if not _is_digit(curr) and _is_digit(nxt):
                out.append(curr.upper())
                out.append("_")
                continue
            if _is_digit(curr) and not _is_digit(nxt):
                out.append(curr.upper())
                out.append("_")
                continue

        out.append(curr.upper())

    return "".join(out)
