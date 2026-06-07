# Individual Progression Reference (mod-individual-progression)

## Overview

`mod-individual-progression` implements a per-player progression system where each player starts at Vanilla (Tier 0) and must defeat end bosses to unlock the next content tier. This creates a personal WoW Classic → TBC → WotLK journey.

> For the full details, read `docs/wikis/mod-individual-progression-wiki/`.
> For the full list of changes, read `docs/wikis/mod-individual-progression-wiki/List-of-Changes.md`.

## Progression Tiers

| Tier | Content | Level Cap | End Boss / Quest |
|------|---------|-----------|-----------------|
| 0 | Starting point (Molten Core) | 60 | — |
| 1 | Onyxia | 60 | Onyxia (simultaneous with Tier 0) |
| 2 | Blackwing Lair | 60 | Nefarian |
| 3 | Pre-AQ (War Effort + ZG) | 60 | Bang a Gong! / Simply Bang a Gong! |
| 4 | AQ War | 60 | Chaos and Destruction quest |
| 5 | Ahn'Qiraj (AQ20 + AQ40) | 60 | C'thun |
| 6 | Naxxramas 40-man | 60 | Kel'thuzad |
| 7 | Pre-TBC (Dark Portal invasion) | 60 | Into the Breach quest |
| 8 | Karazhan, Gruul's, Magtheridon | 70 | Prince Malchezaar |
| 9 | Serpentshrine, Tempest Keep | 70 | Kael'thas |
| 10 | Hyjal, Black Temple | 70 | Illidan |
| 11 | Zul'Aman | 70 | Zul'jin |
| 12 | Sunwell Plateau | 70 | Kil'jaeden |
| 13 | Naxxramas WotLK, EoE, OS | 80 | Kel'thuzad (Level 80 version) |
| 14 | Ulduar | 80 | Yogg-Saron |
| 15 | Trial of the Crusader | 80 | Anub'arak |
| 16 | Icecrown Citadel | 80 | The Lich King |
| 17 | Ruby Sanctum (bonus tier) | 80 | Halion |

> **Important:** The progression value is the **highest tier COMPLETED**, not the current tier being worked on.

## How Progression Works

- Players start at Tier 0/1 (Molten Core + Onyxia simultaneously, as in original Vanilla release)
- Level cap is **60 until Naxxramas completed**, **70 until Sunwell completed**, then **80**
- The module enforces gates: players cannot enter higher-tier content until completing prerequisites
- Players cannot trade/group with those at different progression tiers (configurable)
- Death Knights start at WotLK progression (they skip all pre-WotLK tiers; configurable)

## How Progression Is Stored

The installed module derives a character's progression from hidden rewarded quests,
not from `character_settings`.

- Hidden progression quest IDs are `66000 + progression_state`.
- `GetPlayerProgressionFromQuests()` scans progression states and returns the
  highest hidden quest with `QUEST_STATUS_REWARDED`.
- State `0` is represented by no hidden progression quest.
- The expansion boundary states are:
  - Vanilla: state `< 8` (exact admin boundary target: `0`)
  - TBC: state `>= 8` and `< 13` (exact admin boundary target: `8`)
  - WotLK: state `>= 13` (exact admin boundary target: `13`)
- Natural progression via `UpdateProgressionState()` adds the newly earned hidden
  quest and does not delete older progression quest rows.
- Forced progression via `ForceUpdateProgressionState()` removes all hidden
  progression quests before adding one replacement quest. Treat that as a
  testing/admin command path, not the safest database strategy for preserving
  forward-only character history.

For database inspection, check
`acore_characters.character_queststatus_rewarded` for active quest rows between
`66000` and `66018`. Do not use `character_settings` for this module's current
progression state.

## Setting Player Progression (GM Command)

To manually set a player's progression tier (useful for testing or catching up):
```
# In-game or console (check exact syntax in docs/wikis/azerothcore-wiki/docs/gm-commands.md
# or the mod-individual-progression source for the GM command):
.playerbot setprogression <tier>     # If playerbots-integrated command exists
```

> **Note:** The exact GM command for setting progression is module-specific. Check the module's
> source code or ask in the mod-individual-progression GitHub for the current command syntax.
> For forward-only offline admin promotion, prefer adding the missing hidden
> rewarded quest rows in `character_queststatus_rewarded` and deleting nothing.
> Do not use the old `character_settings` fallback; it is not how this installed
> module stores progression.

## Key Config Options (individualProgression.conf)

> Full options: `docs/configs/individualProgression.conf.dist`

| Key | Description |
|-----|-------------|
| `IndividualProgression.Enable` | Enable/disable the module |
| `IndividualProgression.ReducedDamageAndHealing` | Reduce damage/healing levels 1-60 and 61-70 to approximate original difficulty |
| `IndividualProgression.PreventGroupingWithHigherTier` | Prevent grouping with higher-tier players |
| `IndividualProgression.PreventTradingWithHigherTier` | Prevent trading with higher-tier players |
| `IndividualProgression.AllowDeathKnightAtTierX` | At which tier DKs become available |

## Key Changes Introduced by the Module

**World:**
- Phased content — only appears for players at appropriate tier
- Level cap enforcement (60/70/80 based on progression)
- Vanilla World Boss Lord Kazzak restored
- Vanilla Naxxramas 40-man restored (correct mobs, loot, attunements)
- AQ War effort, Scourge Invasion, Dark Portal invasion events restored
- Original TBC attunements restored
- Rep requirements on items restored

**Classes/Professions:**
- Original starting gear and skill sets (pre-3.1 versions)
- Skill upgrades via item drops (not trainer)
- Original crafting recipe locations and progression
- Original secondary skill progression

**PvP:**
- Simple Vanilla PvP system
- Configurable kills-based ranking
- Original AV version mostly restored

**Economy:**
- Original item versions (Vanilla and TBC stats)
- Badge/emblem rewards appropriate to original tier
- Original vendor inventories

## Useful GM Notes

**AQ War Effort (Tier 3):**
- Every player must complete each resource turn-in at least once
- Then the "complete war effort" quest requires 1000 Commendation Signets
- Can skip Scarab Lord chain with "Simply Bang a Gong!" quest

**Naxxramas Teleporter (Tier 6):**
- Located in Eastern Plaguelands (not the original location — added as a crystal near a Ziggurath)
- Requires attunement through Light's Hope Chapel quests

**DK Progression:**
- DKs start at WotLK progression by default (no pre-WotLK tier gear exists for them)
- Configurable via `IndividualProgression.AllowDeathKnightAtTierX`

## Optional Files

The module includes optional SQL and DBC patches:
- `zz_optional_*` files: stacked changes some players may not want (e.g., smaller vanilla stack sizes, removing heirlooms)
- DBC patches: restore original profession recipes, spell reagents, rogue poisons, original login/loading screens
- Client patch: `patch-V.mpq` (original mana costs) or `patch-S.mpq` (WotLK mana costs) — **use only one**

## Set Progression Level NPC

A community script by Day36512 adds an NPC to set progression:
https://github.com/Day36512/Acore_Lua_Set_Individual_Progression_NPC

This can be installed as a Lua module to allow players or GMs to set progression via an in-game NPC.
