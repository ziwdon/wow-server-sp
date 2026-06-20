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

## Setting Player Progression (Admin UI — preferred)

The admin web app's **Progression page** (`http://<tailscale-ip>:<port>/progression`) is the recommended way to advance a character's expansion tier:

1. Open the Progression page
2. Select the account, then select the character from the list
3. Click the target expansion icon (Classic / TBC / WotLK) — downgrade tiles are grayed and blocked
4. Confirm in the dialog

The character must be offline first. Progression is forward-only.
Under the hood the admin inserts the missing `character_queststatus_rewarded` rows for quest IDs `66001` through `66001 + target_state - 1` — identical to what the module would write after legitimate boss kills.

## Setting Player Progression (Direct DB — fallback)

If the admin app is unavailable, insert the hidden rewarded quest rows directly:

```sql
-- Example: advance character with guid 5 to TBC boundary (state 8)
-- Insert quests 66001–66008 that are not already present
INSERT IGNORE INTO acore_characters.character_queststatus_rewarded (guid, quest, active)
SELECT 5, 66000 + seq, 1
FROM (
  SELECT 1 AS seq UNION SELECT 2 UNION SELECT 3 UNION SELECT 4
  UNION SELECT 5 UNION SELECT 6 UNION SELECT 7 UNION SELECT 8
) AS states;
```

Always INSERT IGNORE (never delete existing rows) — removing rows can confuse the module's `GetPlayerProgressionFromQuests()` scan. Do not use `character_settings` for progression state; this installed version of the module ignores that column.

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

**Onyxia 40-man (Tier 0/1) — entry mechanic:**
- Entrance area trigger is in Dustwallow Marsh at the Onyxia's Lair cave (area trigger 2848, map 1)
- Requires the **Drakefire Amulet** (item 11086) in your bags — obtained via the Onyxia attunement quest chain (Alliance: Marshal Maxwell in Morgan's Vigil; Horde: Warlord Goretooth in Kargath)
- UBRS attunement (Seal of Ascension) is also required as part of the chain
- The mod uses the **10-man Heroic** difficulty slot (`RAID_DIFFICULTY_10MAN_HEROIC`) for the 40-man — **not** 25-man Normal
- When a level ≤ 70 character with the amulet walks through the entrance trigger, the script **automatically forces your raid difficulty to 10-man Heroic** and teleports you in — regardless of what you had selected in the UI
- The WotLK 10-man and 25-man Normal versions are gated until WotLK progression tier (state ≥ 13)
- To confirm you're in the 40-man vanilla version: Onyxia should be **level 63** (not 83), with Onyxian Warders and Guards as vanilla-era mobs
- Source: `modules/mod-individual-progression/src/vanillaScripts/instance_onyxias_lair.cpp` — `onyxia_entrance_trigger::OnTrigger`

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
