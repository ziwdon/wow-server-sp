import re
from pathlib import Path


STYLESHEET = Path(__file__).resolve().parents[1] / "app/static/app.css"
VAR_REFERENCE = re.compile(r"var\(\s*(--[\w-]+)")


def _closing_parenthesis(css: str, opening_index: int) -> int:
    depth = 0
    for index in range(opening_index, len(css)):
        if css[index] == "(":
            depth += 1
        elif css[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return len(css)


def _has_own_fallback(css: str, argument_start: int, closing_index: int) -> bool:
    depth = 0
    for character in css[argument_start:closing_index]:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            return True
    return False


def _undefined_custom_properties(css: str, defined: set[str]) -> set[str]:
    unresolved = set()
    for match in VAR_REFERENCE.finditer(css):
        closing_index = _closing_parenthesis(css, match.start())
        has_fallback = _has_own_fallback(css, match.end(), closing_index)
        if match.group(1) not in defined and not has_fallback:
            unresolved.add(match.group(1))
    return unresolved


def test_stylesheet_custom_properties_are_defined_or_have_fallbacks():
    """Keep CSS token typos from silently falling back to invalid declarations."""
    css = STYLESHEET.read_text()
    defined = set(re.findall(r"(?m)^\s*(--[\w-]+)\s*:", css))

    assert _undefined_custom_properties(css, defined) == set()


def test_nested_custom_property_reference_needs_its_own_fallback():
    css = ".example { color: var(--outer, var(--nested-typo)); }"

    assert _undefined_custom_properties(css, set()) == {"--nested-typo"}
