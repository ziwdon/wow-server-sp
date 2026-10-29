"""Static WoW 3.3.5a reference names for the admin's stats, players and armory pages."""

import time
from datetime import datetime, timezone

CLASSES = {
    1: "Warrior",
    2: "Paladin",
    3: "Hunter",
    4: "Rogue",
    5: "Priest",
    6: "Death Knight",
    7: "Shaman",
    8: "Mage",
    9: "Warlock",
    11: "Druid",
}

RACES = {
    1: "Human",
    2: "Orc",
    3: "Dwarf",
    4: "Night Elf",
    5: "Undead",
    6: "Tauren",
    7: "Gnome",
    8: "Troll",
    10: "Blood Elf",
    11: "Draenei",
}

RACE_FACTION = {
    1: "Alliance",
    2: "Horde",
    3: "Alliance",
    4: "Alliance",
    5: "Horde",
    6: "Horde",
    7: "Alliance",
    8: "Horde",
    10: "Horde",
    11: "Alliance",
}

CLASS_COLORS = {
    "Death Knight": "#C41E3A",
    "Druid":        "#FF7C0A",
    "Hunter":       "#AAD372",
    "Mage":         "#3FC7EB",
    "Paladin":      "#F48CBA",
    "Priest":       "#D2C8B4",
    "Rogue":        "#FFF468",
    "Shaman":       "#0070DD",
    "Warlock":      "#8788EE",
    "Warrior":      "#C69B6D",
}

FACTION_COLORS = {
    "Alliance": "#4080C0",
    "Horde":    "#C03030",
    "Unknown":  "#C8B020",
}

ZONES = {
    1: "Dun Morogh",
    3: "Badlands",
    4: "Blasted Lands",
    8: "Swamp of Sorrows",
    10: "Duskwood",
    11: "Wetlands",
    12: "Elwynn Forest",
    14: "Durotar",
    15: "Dustwallow Marsh",
    16: "Azshara",
    17: "The Barrens",
    21: "Kul Tiras (inaccessible)",
    22: "Programmer Isle (inaccessible)",
    25: "Blackrock Mountain",
    28: "Western Plaguelands",
    30: "Nine (inaccessible)",
    33: "Stranglethorn Vale",
    36: "Alterac Mountains",
    38: "Loch Modan",
    40: "Westfall",
    41: "Deadwind Pass",
    44: "Redridge Mountains",
    45: "Arathi Highlands",
    46: "Burning Steppes",
    47: "The Hinterlands",
    51: "Searing Gorge",
    65: "Dragonblight",
    66: "Zul'Drak",
    67: "The Storm Peaks",
    85: "Tirisfal Glades",
    130: "Silverpine Forest",
    139: "Eastern Plaguelands",
    141: "Teldrassil",
    148: "Darkshore",
    151: "Designer Island (inaccessible)",
    170: "Lordamere Lake",
    206: "Utgarde Keep",
    207: "The Great Sea",
    208: "Unused Ironcladcove (inaccessible)",
    209: "Shadowfang Keep",
    210: "Icecrown",
    214: "The Great Sea",
    215: "Mulgore",
    267: "Hillsbrad Foothills",
    268: "Azshara Crater",
    269: "Dun Algaz",
    276: "UNUSED Stonewrought Pass (inaccessible)",
    293: "Thoradin's Wall",
    296: "South Seas UNUSED (inaccessible)",
    308: "The Forbidding Sea",
    330: "Thandol Span",
    331: "Ashenvale",
    332: "The Great Sea",
    357: "Feralas",
    361: "Felwood",
    377: "Southfury River",
    394: "Grizzly Hills",
    400: "Thousand Needles",
    405: "Desolace",
    406: "Stonetalon Mountains",
    408: "Gillijim's Isle",
    409: "Island of Doctor Lapidis",
    440: "Tanaris",
    457: "The Veiled Sea",
    490: "Un'Goro Crater",
    491: "Razorfen Kraul",
    493: "Moonglade",
    495: "Howling Fjord",
    616: "Hyjal",
    618: "Winterspring",
    676: "Outland",
    717: "The Stockade",
    718: "Wailing Caverns",
    719: "Blackfathom Deeps",
    721: "Gnomeregan",
    722: "Razorfen Downs",
    796: "Scarlet Monastery",
    876: "GM Island (inaccessible)",
    956: "The Verdant Fields (inaccessible)",
    1176: "Zul'Farrak",
    1196: "Utgarde Pinnacle",
    1337: "Uldaman",
    1377: "Silithus",
    1397: "Emerald Forest (inaccessible)",
    1417: "Sunken Temple",
    1477: "The Temple of Atal'Hakkar",
    1497: "Undercity",
    1519: "Stormwind City",
    1537: "Ironforge",
    1579: "Unused The Deadmines 002 (inaccessible)",
    1581: "The Deadmines",
    1583: "Blackrock Spire",
    1584: "Blackrock Depths",
    1637: "Orgrimmar",
    1638: "Thunder Bluff",
    1657: "Darnassus",
    1941: "Caverns of Time",
    1977: "Zul'Gurub",
    2017: "Stratholme",
    2037: "Quel'Thalas",
    2057: "Scholomance",
    2100: "Maraudon",
    2159: "Onyxia's Lair",
    2257: "Deeprun Tram",
    2280: "DO NOT USE (inaccessible)",
    2366: "The Black Morass",
    2367: "Old Hillsbrad Foothills",
    2437: "Ragefire Chasm",
    2557: "Dire Maul",
    2597: "Alterac Valley",
    2677: "Blackwing Lair",
    2717: "Molten Core",
    2817: "Crystalsong Forest",
    2917: "Hall of Legends",
    2918: "Champions' Hall",
    3277: "Warsong Gulch",
    3358: "Arathi Basin",
    3428: "Ahn'Qiraj",
    3429: "Ruins of Ahn'Qiraj",
    3430: "Eversong Woods",
    3433: "Ghostlands",
    3455: "The North Sea",
    3456: "Naxxramas",
    3457: "Karazhan",
    3459: "City (inaccessible)",
    3477: "Azjol-Nerub",
    3478: "Gates of Ahn'Qiraj",
    3479: "The Veiled Sea",
    3483: "Hellfire Peninsula",
    3487: "Silvermoon City",
    3518: "Nagrand",
    3519: "Terokkar Forest",
    3520: "Shadowmoon Valley",
    3521: "Zangarmarsh",
    3522: "Blade's Edge Mountains",
    3523: "Netherstorm",
    3524: "Azuremyst Isle",
    3525: "Bloodmyst Isle",
    3535: "Hellfire Citadel",
    3537: "Borean Tundra",
    3540: "Twisting Nether (inaccessible)",
    3557: "The Exodar",
    3562: "Hellfire Ramparts",
    3563: "Hellfire Citadel",
    3605: "Hyjal Past",
    3606: "Hyjal Summit",
    3607: "Serpentshrine Cavern",
    3698: "Nagrand Arena",
    3702: "Blade's Edge Arena",
    3703: "Shattrath City",
    3711: "Sholazar Basin",
    3713: "The Blood Furnace",
    3714: "The Shattered Halls",
    3715: "The Steamvault",
    3716: "The Underbog",
    3717: "The Slave Pens",
    3789: "Shadow Labyrinth",
    3790: "Auchenai Crypts",
    3791: "Sethekk Halls",
    3792: "Mana-Tombs",
    3805: "Zul'Aman",
    3817: "Testing (inaccessible)",
    3820: "Eye of the Storm",
    3836: "Magtheridon's Lair",
    3842: "Tempest Keep",
    3845: "The Botanica",
    3847: "The Mechanar",
    3848: "The Arcatraz",
    3849: "The Mechanar",
    3917: "Auchindoun",
    3923: "Gruul's Lair",
    3948: "Brian and Pat Test (inaccessible)",
    3959: "Black Temple",
    3968: "Ruins of Lordaeron",
    3979: "The Frozen Sea",
    4019: "Development Land (inaccessible)",
    4075: "Sunwell Plateau",
    4076: "Reuse Me 7 (inaccessible)",
    4080: "Isle of Quel'Danas",
    4100: "The Culling of Stratholme",
    4131: "Magisters' Terrace",
    4196: "Drak'Tharon Keep",
    4197: "Wintergrasp",
    4201: "Vordrassil's Tears",
    4228: "The Oculus",
    4258: "The North Sea",
    4264: "Halls of Stone",
    4265: "The Nexus",
    4272: "Halls of Lightning",
    4273: "Ulduar",
    4277: "Azjol-Nerub",
    4298: "Plaguelands: The Scarlet Enclave",
    4378: "Dalaran Sewers",
    4384: "Strand of the Ancients",
    4395: "Dalaran",
    4406: "The Ring of Valor",
    4415: "The Violet Hold",
    4416: "Gundrak",
    4493: "The Obsidian Sanctum",
    4494: "Ahn'kahet: The Old Kingdom",
    4500: "The Eye of Eternity",
    4602: "Force Interior (inaccessible)",
    4603: "Vault of Archavon",
    4630: "The North Sea",
    4710: "Isle of Conquest",
    4722: "Trial of the Crusader",
    4723: "Trial of the Champion",
    4742: "Hrothgar's Landing",
    4763: "Transport: Alliance Gunship",
    4764: "Transport: Horde Gunship",
    4809: "The Forge of Souls",
    4812: "Icecrown Citadel",
    4813: "Pit of Saron",
    4820: "Halls of Reflection",
    4832: "Transport: Alliance Gunship (IGB)",
    4833: "Transport: Horde Gunship (IGB)",
    4893: "The Frost Queen's Lair",
    4894: "Putricide's Laboratory of Alchemical Horrors and Fun",
    4895: "The Crimson Hall",
    4896: "The Frozen Throne",
    4897: "The Sanctum of Blood",
    4987: "The Ruby Sanctum",
}

# --- Armory: items, stats and powers (WoW 3.3.5a) ---
# Ids are the pinned core's enums (rev 3f2969dd); names are what the 3.3.5 client shows.

# SharedDefines.h ItemQualities / ItemQualityColors (7 = heirloom shares the artifact colour)
QUALITY_COLORS = {
    0: "#9d9d9d",
    1: "#ffffff",
    2: "#1eff00",
    3: "#0070dd",
    4: "#a335ee",
    5: "#ff8000",
    6: "#e6cc80",
    7: "#e6cc80",
}

QUALITY_NAMES = {
    0: "Poor",
    1: "Common",
    2: "Uncommon",
    3: "Rare",
    4: "Epic",
    5: "Legendary",
    6: "Artifact",
    7: "Heirloom",
}

# ItemTemplate.h InventoryType -> the client's INVTYPE_* strings (the tooltip's slot line;
# the weapon/armour kind, e.g. "Shield" or "Bow", comes from ITEM_SUBCLASS_NAMES)
INVENTORY_TYPE_NAMES = {
    0: "Non-equippable",
    1: "Head",
    2: "Neck",
    3: "Shoulder",
    4: "Shirt",
    5: "Chest",
    6: "Waist",
    7: "Legs",
    8: "Feet",
    9: "Wrist",
    10: "Hands",
    11: "Finger",
    12: "Trinket",
    13: "One-Hand",
    14: "Off Hand",          # INVTYPE_SHIELD
    15: "Ranged",            # INVTYPE_RANGED (bows)
    16: "Back",
    17: "Two-Hand",
    18: "Bag",
    19: "Tabard",
    20: "Chest",             # INVTYPE_ROBE
    21: "Main Hand",
    22: "Off Hand",
    23: "Held In Off-hand",
    24: "Ammo",
    25: "Thrown",
    26: "Ranged",            # INVTYPE_RANGEDRIGHT (guns, crossbows, wands)
    27: "Quiver",
    28: "Relic",
}

# (item class, subclass): ItemTemplate.h ItemSubclassWeapon (class 2) / ItemSubclassArmor (4)
ITEM_SUBCLASS_NAMES = {
    (2, 0): "Axe",
    (2, 1): "Axe",
    (2, 2): "Bow",
    (2, 3): "Gun",
    (2, 4): "Mace",
    (2, 5): "Mace",
    (2, 6): "Polearm",
    (2, 7): "Sword",
    (2, 8): "Sword",
    (2, 9): "Obsolete",
    (2, 10): "Staff",
    (2, 11): "Exotic",
    (2, 12): "Exotic",
    (2, 13): "Fist Weapon",
    (2, 14): "Miscellaneous",
    (2, 15): "Dagger",
    (2, 16): "Thrown",
    (2, 17): "Spear",
    (2, 18): "Crossbow",
    (2, 19): "Wand",
    (2, 20): "Fishing Pole",
    (4, 0): "Miscellaneous",
    (4, 1): "Cloth",
    (4, 2): "Leather",
    (4, 3): "Mail",
    (4, 4): "Plate",
    (4, 5): "Buckler",
    (4, 6): "Shield",
    (4, 7): "Libram",
    (4, 8): "Idol",
    (4, 9): "Totem",
    (4, 10): "Sigil",
}

# ItemTemplate.h ItemModType (item_template.stat_typeN); 40 (feral AP) is "not in 3.3" there
STAT_NAMES = {
    0: "Mana",
    1: "Health",
    3: "Agility",
    4: "Strength",
    5: "Intellect",
    6: "Spirit",
    7: "Stamina",
    12: "Defense Rating",
    13: "Dodge Rating",
    14: "Parry Rating",
    15: "Block Rating",
    16: "Melee Hit Rating",
    17: "Ranged Hit Rating",
    18: "Spell Hit Rating",
    19: "Melee Critical Strike Rating",
    20: "Ranged Critical Strike Rating",
    21: "Spell Critical Strike Rating",
    22: "Melee Hit Avoidance Rating",
    23: "Ranged Hit Avoidance Rating",
    24: "Spell Hit Avoidance Rating",
    25: "Melee Critical Avoidance Rating",
    26: "Ranged Critical Avoidance Rating",
    27: "Spell Critical Avoidance Rating",
    28: "Melee Haste Rating",
    29: "Ranged Haste Rating",
    30: "Spell Haste Rating",
    31: "Hit Rating",
    32: "Critical Strike Rating",
    33: "Hit Avoidance Rating",
    34: "Critical Strike Avoidance Rating",
    35: "Resilience Rating",
    36: "Haste Rating",
    37: "Expertise Rating",
    38: "Attack Power",
    39: "Ranged Attack Power",
    41: "Spell Healing",
    42: "Spell Damage",
    43: "Mana Regeneration",
    44: "Armor Penetration Rating",
    45: "Spell Power",
    46: "Health Regeneration",
    47: "Spell Penetration",
    48: "Block Value",
}

# SharedDefines.h SpellSchools (item_template.dmg_typeN)
DAMAGE_TYPE_NAMES = {
    0: "Physical",
    1: "Holy",
    2: "Fire",
    3: "Nature",
    4: "Frost",
    5: "Shadow",
    6: "Arcane",
}

# Player.h EquipmentSlots: character_inventory.slot for bag 0, slots 0-18
EQUIP_SLOT_NAMES = {
    0: "Head",
    1: "Neck",
    2: "Shoulder",
    3: "Shirt",
    4: "Chest",
    5: "Waist",
    6: "Legs",
    7: "Feet",
    8: "Wrist",
    9: "Hands",
    10: "Finger 1",
    11: "Finger 2",
    12: "Trinket 1",
    13: "Trinket 2",
    14: "Back",
    15: "Main Hand",
    16: "Off Hand",
    17: "Ranged",
    18: "Tabard",
}

# ItemTemplate.h ItemBondingType (0 = no bind, no line); 5 is "not used in game" there
BONDING_NAMES = {
    1: "Binds when picked up",
    2: "Binds when equipped",
    3: "Binds when used",
    4: "Quest Item",
    5: "Quest Item",
}

# SharedDefines.h Powers: character_stats.maxpower<N> holds power type N - 1
POWER_NAMES = {
    0: "Mana",
    1: "Rage",
    2: "Focus",
    3: "Energy",
    4: "Happiness",
    5: "Runes",
    6: "Runic power",
}

# The power bar each class shows (ChrClasses.dbc DisplayPower). Every player saves max
# rage and max energy whatever its class (Player::InitStatsForLevel), so the class, not a
# non-zero value, decides which one the armory shows.
CLASS_POWER_TYPE = {
    1: 1,   # Warrior: rage
    2: 0,   # Paladin: mana
    3: 0,   # Hunter
    4: 3,   # Rogue: energy
    5: 0,   # Priest
    6: 6,   # Death Knight: runic power
    7: 0,   # Shaman
    8: 0,   # Mage
    9: 0,   # Warlock
    11: 0,  # Druid (caster form)
}


def class_name(i: int) -> str:
    return CLASSES.get(i, f"Class {i}")


def race_name(i: int) -> str:
    return RACES.get(i, f"Race {i}")


def zone_name(i: int) -> str:
    return ZONES.get(i, f"Zone {i}")


def faction(race_id: int) -> str:
    return RACE_FACTION.get(race_id, "Unknown")


def class_color(name: str) -> str:
    return CLASS_COLORS.get(name, "")


def faction_color(name: str) -> str:
    return FACTION_COLORS.get(name, "")


def relative_last_online(logout_time: int, online_now: bool, now: float | None = None) -> str:
    """Human-readable "last online" label for a character.

    online_now             → "online" (confirmed human session: CharRow.online_now,
                              the same presence rule as the Online-now card)
    otherwise              → falls through to the logout_time-based label, so an
                              online=1 alt-bot row never shows "online"
    logout_time == 0       → "never"  (character has never logged in)
    same UTC date          → "today"; 1 day → "yesterday"; N days → "N days ago"
    Future logout_time (clock skew) clamps to 0 days → "today".
    """
    if online_now:
        return "online"
    if not logout_time:
        return "never"
    if now is None:
        now = time.time()
    now_date = datetime.fromtimestamp(now, timezone.utc).date()
    logout_date = datetime.fromtimestamp(logout_time, timezone.utc).date()
    days = max(0, (now_date - logout_date).days)
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"
