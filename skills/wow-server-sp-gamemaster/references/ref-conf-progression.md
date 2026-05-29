# individualProgression.conf Reference
> Source of truth: `docs/configs/individualProgression.conf.dist`
> Note: EnablePlayerSettings = 1 must be set in worldserver.conf for per-player progression to work. (IndividualProgression.SimpleConfigOverride = 1 handles this automatically if enabled.)

---

## CORE ENABLE / GROUP RULES

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.Enable | 1 | Enable the Individual Progression module (world DB changes persist even if disabled) |
| IndividualProgression.EnforceGroupRules | 1 | Only allow players in the same progression phase to group together |

---

## POWER ADJUSTMENTS (VANILLA / TBC)

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.VanillaPowerAdjustment | 1 | Attack power multiplier during Vanilla content (levels 11–60, applied linearly); suggested 0.5–0.6 |
| IndividualProgression.VanillaHealingAdjustment | 1 | Healing power multiplier during Vanilla content; suggested 0.5 |
| IndividualProgression.TBCPowerAdjustment | 1 | Attack power multiplier during TBC content (flat, not linear); suggested 0.5–0.6 |
| IndividualProgression.TBCHealingAdjustment | 1 | Healing power multiplier during TBC content; suggested 0.5–0.6 |
| IndividualProgression.BotOnlyAdjustments | 0 | Apply Vanilla/TBC power adjustments to RNDbots only in dungeons/raids, leaving real players unaffected |

---

## QUEST & FISHING BEHAVIOUR

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.QuestXPFix | 1 | Reduce Vanilla/TBC quest XP to pre-patch values (undoing the 2.3/3.0 XP boost catchup changes) |
| IndividualProgression.FishingFix | 1 | Restore pre-3.1 fishing progression (must level in low-level zones before higher zones grant skill) |
| IndividualProgression.QuestMoneyAtLevelCap | 1 | Grant extra money for quest completions at the current progression stage level cap (added in patch 1.10) |
| IndividualProgression.RepeatableVanillaQuestsXP | 1 | Allow repeatable Vanilla quests to grant XP on every turn-in (Vanilla behaviour; WotLK default is once only) |
| IndividualProgression.DisableQuestMarkers | 1 | Disable quest object markers and sparkles (added in patch 2.3) |

---

## INSTANCE ACCESS & ENCOUNTER TWEAKS

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.RequireNaxxStrathEntrance | 0 | Require entering Naxx 40 through the original Stratholme entrance before the EPL teleport crystal works |
| IndividualProgression.doableNaxx40Bosses | 0 | Ease several Naxx 40 bosses: Razuvious casts Disrupting Shout, Four Horsemen do reduced damage, Gluth zombie timer increased |
| IndividualProgression.MoltenCore.ManualRuneHandling | 1 | Require manual dousing of MC runes via Aqual Quintessence (Vanilla-like); 0 = automatic on boss death |
| IndividualProgression.MoltenCore.AqualEssenceCooldownReduction | 0 | Reduce Eternal Quintessence cooldown by this many minutes (60 = no cooldown) |
| IndividualProgression.SerpentshrineCavern.RequireAllBosses | 1 | Require killing all SSC bosses before Lady Vashj's console panel is accessible |
| IndividualProgression.TheEye.RequireAllBosses | 1 | Require killing all The Eye bosses before Kael'thas' doors open |
| IndividualProgression.AllowEarlyDungeonSet2 | 0 | Allow Dungeon Set 2 content to be accessed before it is normally available in progression |
| IndividualProgression.AllowEarlyScourgeBosses | 0 | Allow the 6 Scourge Invasion dungeon bosses to be fought outside their normal progression tier |

---

## UI / LFG / MONSTER SIGHT

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.DisableRDF | 0 | Disable Random Dungeon Finder within individual progression (specific dungeon queues and holiday events remain) |
| IndividualProgression.MaxMonsterSight | 1 | Increase monster sight range from 50 to 80 yards (needed for AV tower/bunker archers) |
| IndividualProgression.SimpleConfigOverride | 1 | Let this module auto-set PlayerSettings, DBC item attributes, and water breath timer (1 min Vanilla value) |

---

## PROGRESSION STAGE CONTROL

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.ProgressionLimit | 0 | Cap players at this stage (0 = no cap; max stage is 18 = end of WotLK) |
| IndividualProgression.StartingProgression | 0 | Force new (and existing) characters to start at this stage on next login |
| IndividualProgression.DisableDefaultProgression | 0 | Disable the standard kill-based progression flow; advance only via custom creature entries |
| IndividualProgression.CustomProgression | "" | `creatureID:stage` pairs for custom progression triggers (e.g. `"448 8, 639 13"`); leave empty to disable |

---

## ARENA SEASONS

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.TBC.ArenaSeason | 1 | Current TBC arena season (1–4) |
| IndividualProgression.WotLK.ArenaSeason | 5 | Current WotLK arena season (5–8) |

---

## CLASS / RACE UNLOCK PROGRESSION

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.TbcRacesUnlockProgression | 0 | Stage required to unlock TBC races (Draenei, Blood Elf) for character creation; 0 = always available |
| IndividualProgression.tbcRacesStartingProgression | 0 | Progression stage Draenei and Blood Elf characters start at |
| IndividualProgression.DeathKnightUnlockProgression | 13 | Stage required to create Death Knights; 0 = always available; default requires TBC completion |
| IndividualProgression.DeathKnightStartingProgression | 13 | Progression stage Death Knight characters start at |

---

## CONTENT UNLOCK STAGES

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.RequiredZulGurubProgression | 3 | Stage at which Zul'Gurub becomes accessible (default: after BWL) |

---

## PVP — VANILLA RANK KILL REQUIREMENTS

> Pattern: `IndividualProgression.VanillaPvpKillRequirement.Rank<N> = <kills>`
> Ranks 1–14; represents required PvP honor kills per rank to earn titles and access rank-gated items.

| Key | Default |
|-----|---------|
| IndividualProgression.VanillaPvpKillRequirement.Rank1 | 100 |
| IndividualProgression.VanillaPvpKillRequirement.Rank2 | 200 |
| IndividualProgression.VanillaPvpKillRequirement.Rank3 | 400 |
| IndividualProgression.VanillaPvpKillRequirement.Rank4 | 800 |
| IndividualProgression.VanillaPvpKillRequirement.Rank5 | 1400 |
| IndividualProgression.VanillaPvpKillRequirement.Rank6 | 2000 |
| IndividualProgression.VanillaPvpKillRequirement.Rank7 | 3000 |
| IndividualProgression.VanillaPvpKillRequirement.Rank8 | 4500 |
| IndividualProgression.VanillaPvpKillRequirement.Rank9 | 6000 |
| IndividualProgression.VanillaPvpKillRequirement.Rank10 | 8000 |
| IndividualProgression.VanillaPvpKillRequirement.Rank11 | 10000 |
| IndividualProgression.VanillaPvpKillRequirement.Rank12 | 13000 |
| IndividualProgression.VanillaPvpKillRequirement.Rank13 | 18000 |
| IndividualProgression.VanillaPvpKillRequirement.Rank14 | 24000 |

---

## PVP — TITLE PERSISTENCE

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.VanillaPvpTitlesPersistAfterVanilla | 1 | Players retain Vanilla PvP titles after reaching TBC/WotLK (Blizzlike) |
| IndividualProgression.VanillaPvpEarnTitlesAfterVanilla | 0 | Players can continue earning new Vanilla PvP titles after reaching TBC/WotLK (non-Blizzlike) |

---

## BOT ACCOUNT SETTINGS

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.BotAccountsEarnPvPTitles | 0 | Allow bot accounts to earn PvP titles |
| IndividualProgression.BotAccountsMaxLevel | 80 | Maximum level allowed for bot accounts |
| IndividualProgression.BotAccountsRegex | "^RNDBOT.*" | Regex matching bot account names excluded from individual progression; use `(^RNDBOT.*\|NAME)` for multiples |
| IndividualProgression.ExcludedAccountsRegex | "" | Regex matching any additional accounts to exclude from individual progression (testing etc.) |

---

## REPUTATION SHARING COMMAND

| Key | Default | Description |
|-----|---------|-------------|
| IndividualProgression.EnableSetRepCommand | 0 | Enable `.ip setrep` command for normal and GM accounts to share faction rep across account characters |
| IndividualProgression.LimitedSetRepCommand | 1 | Require minimum standing (neutral/friendly/honored, faction-dependent) before rep can be shared to prevent abuse |
| IndividualProgression.sharedFactionIdsRegex | "59\|270\|349\|509\|510\|529\|576\|589\|609\|729\|730\|749\|889\|890\|909" | Pipe-separated faction IDs eligible for `.ip setrep`; default covers Vanilla factions only |

> Full faction ID reference:
> - Vanilla: `59|270|349|509|510|529|576|589|609|729|730|749|889|890|909|910`
> - TBC: `922|932|933|934|935|941|942|946|947|967|970|978|989|990|1011|1012|1015|1031|1038|1077`
> - WotLK: `1037|1052|1073|1090|1091|1094|1098|1119|1124|1156`
> See also: https://www.wowhead.com/wotlk/factions
