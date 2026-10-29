"""Players page: character names link to the armory with no visible change (spec 5).

The name markup (text, medal and its ``style="color: …"`` span) must stay exactly what the
template rendered before the links existed; the only addition is the wrapping
``<a class="char-link" href="/armory/<guid>">`` whose CSS inherits the colour and shows an
underline only on hover.
"""

import re
from pathlib import Path

from app.main import templates
from app.services.players import AccountGroup, CharRow, PlayersSnapshot, PvpRankRow, RankRow

STYLESHEET = Path(__file__).resolve().parents[1] / "app" / "static" / "app.css"

SARIEL = CharRow(
    account="CARLOS", name="Sariel", class_name="Druid", class_color="#FF7C0A",
    race_name="Night Elf", faction="Alliance", faction_color="#4080C0",
    level=25, online=True, zone_name="Stormwind City", latency=8, last_logout=0,
    guid=2506, online_now=True,
)

SNAPSHOT = PlayersSnapshot(
    fetched_at=1716144665.0,
    total_players=1, online_players=1,
    cap_vanilla=0, cap_tbc=0, cap_wotlk=0,
    online_now=(SARIEL,),
    all_groups=(AccountGroup("CARLOS", (SARIEL,)),),
    top_pve=(
        RankRow(1, "Vegivaca", "Warrior", "#C69B6D", "Tauren", "Horde", "#C03030", 58, 37, 31),
        RankRow(2, "Sariel", "Druid", "#FF7C0A", "Night Elf", "Alliance", "#4080C0", 25, None, 2506),
    ),
    top_pvp=(
        PvpRankRow(1, "Pitocas", "Hunter", "#AAD372", "Gnome", "Alliance", "#4080C0", 42, 1500, 44),
    ),
)

# Every cell of every data row as the template rendered SNAPSHOT before the links were added
# (captured from the pre-change template), with the guid each row's name must link to.
PRE_CHANGE_ROWS = [
    ("online", 2506, [
        '<span>Carlos (<span style="color: #FF7C0A">Sariel</span>)</span>',
        '<span style="color: #FF7C0A">Druid</span>',
        "<span>Night Elf</span>",
        "<span>25</span>",
        '<span style="color: #4080C0">Alliance</span>',
        "<span>Stormwind City</span>",
    ]),
    ("all", 2506, [
        '<span style="color: #FF7C0A">Sariel</span>',
        '<span style="color: #FF7C0A">Druid</span>',
        "<span>Night Elf</span>",
        "<span>25</span>",
        '<span style="color: #4080C0">Alliance</span>',
        "<span>online</span>",
    ]),
    ("rank", 31, [
        "<span>1</span>",
        '<span style="color: #C69B6D">🥇 Vegivaca</span>',
        '<span style="color: #C69B6D">Warrior</span>',
        "<span>Tauren</span>",
        '<span style="color: #C03030">Horde</span>',
        '<span class="num">58</span>',
        '<span class="num">37</span>',
    ]),
    ("rank", 2506, [
        "<span>2</span>",
        '<span style="color: #FF7C0A">🥈 Sariel</span>',
        '<span style="color: #FF7C0A">Druid</span>',
        "<span>Night Elf</span>",
        '<span style="color: #4080C0">Alliance</span>',
        '<span class="num">25</span>',
        '<span class="num">—</span>',
    ]),
    ("rank-pvp", 44, [
        "<span>1</span>",
        '<span style="color: #AAD372">🥇 Pitocas</span>',
        '<span style="color: #AAD372">Hunter</span>',
        "<span>Gnome</span>",
        '<span style="color: #4080C0">Alliance</span>',
        '<span class="num">42</span>',
        '<span class="num">1500</span>',
    ]),
]


def _render(snap: PlayersSnapshot) -> str:
    return templates.env.get_template("partials/players_page.html").render(
        snap=snap, error=None, last_refreshed=None,
    )


def _data_rows(html: str) -> list[tuple[str, list[str]]]:
    rows = re.findall(
        r'<div class="player-row player-row--(online|all|rank|rank-pvp)">(.*?)</div>', html, re.S,
    )
    return [(kind, [line.strip() for line in body.strip().splitlines()]) for kind, body in rows]


def _link(guid: int, inner: str) -> str:
    return f'<a class="char-link" href="/armory/{guid}">{inner}</a>'


def _expected_after(kind: str, guid: int, cells: list[str]) -> list[str]:
    after = list(cells)
    if kind == "online":
        # "Account (Character)": only the character part is the link.
        name = '<span style="color: #FF7C0A">Sariel</span>'
        after[0] = after[0].replace(name, _link(guid, name))
    elif kind == "all":
        after[0] = _link(guid, after[0])
    else:
        after[1] = _link(guid, after[1])
    return after


def test_players_page_names_link_to_armory_without_changing_text():
    html = _render(SNAPSHOT)
    rows = _data_rows(html)

    assert [kind for kind, _ in rows] == [kind for kind, _, _ in PRE_CHANGE_ROWS]
    for (kind, cells), (_, guid, before) in zip(rows, PRE_CHANGE_ROWS):
        assert cells == _expected_after(kind, guid, before)

    assert 'href="/armory/2506"' in html
    # One link per data row, none on the header rows, nothing else on the page links.
    assert html.count('class="char-link"') == len(PRE_CHANGE_ROWS)
    assert html.count("<a ") == len(PRE_CHANGE_ROWS)

    # With the link wrappers removed the rows are byte-identical to the pre-change render.
    unwrapped = re.sub(r'<a class="char-link" href="/armory/\d+">|</a>', "", html)
    assert [cells for _, cells in _data_rows(unwrapped)] == [b for _, _, b in PRE_CHANGE_ROWS]


def _declarations(css: str, selector: str) -> set[str]:
    match = re.search(r"^" + re.escape(selector) + r"\s*\{([^}]*)\}", css, re.M)
    assert match, f"no rule for {selector}"
    return {d.strip() for d in match.group(1).split(";") if d.strip()}


def test_linked_name_cells_keep_the_cell_overflow_rule():
    """A wrapped cell's grid item is now the <a>; the coloured span inside it keeps exactly the
    span cells' clipping (as a block, so it clips), so a clipped name's "…" is drawn in the
    class colour as before the link (an ellipsis takes the colour of the box that clips; the
    browser check, tests/browser/armory_check.py, measured the anchor's row colour otherwise).
    The <a> only clips."""
    css = STYLESHEET.read_text()
    span_cell = _declarations(css, ".player-row > span")
    assert span_cell == {"overflow: hidden", "text-overflow: ellipsis", "white-space: nowrap"}
    assert _declarations(css, ".player-row > .char-link > span") == span_cell | {"display: block"}
    assert _declarations(css, ".player-row > .char-link") == {"min-width: 0", "overflow: hidden"}


def test_char_link_inherits_colour_and_underlines_only_on_hover():
    css = STYLESHEET.read_text()
    assert re.search(r"\.char-link \{ color: inherit; text-decoration: none; \}", css)
    assert re.search(r"\.char-link:hover \{ text-decoration: underline; cursor: pointer; \}", css)
