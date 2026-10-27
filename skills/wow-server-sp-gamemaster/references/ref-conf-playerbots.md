# playerbots.conf Reference
> Source of truth: the pinned checkout's `modules/mod-playerbots/conf/playerbots.conf.dist` (the admin Settings page lists the same keys).
> Set these via AC_* env vars in `docker-compose.override.yml` or edit configs/modules/playerbots.conf directly.
> Note: env vars override conf values; the AC_* derivation rule applies (see ref-config-worldserver.md).
>
> **Renamed prefix.** mod-playerbots #2854 renamed every `AiPlayerbot.X` key to `Playerbots.X` (no fallback; old keys are silently ignored). Keys below use the new prefix, and env vars are `AC_PLAYERBOTS_<X>` (was `AC_AI_PLAYERBOT_<X>`).
>
> **Snapshot caveat.** The defaults and descriptions in this file were written against the pre-rename `playerbots.conf.dist`; only the prefix was updated mechanically. This file was never exhaustive: it covers about 346 of the 891 `Playerbots.*` keys in the pinned dist (e.g. it omits `Playerbots.LevelBrackets.*`, `Playerbots.AutoPartyBuffs`, `Playerbots.AutoGreaterBlessings`, `Playerbots.EquipAndSpecPersistence[Level]`, `Playerbots.BotSendMailEnabled`). Ten keys listed here no longer exist in the pinned dist (flagged "not in the pinned dist" below), and two rows have a different default there (flagged "default changed": `AutoLearnQuestSpells`, `UnobtainableItems`). For anything that matters, read the pinned dist or the Settings page rather than trusting a default quoted here.

---

## GENERAL SETTINGS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.Enabled | 1 | Enable or disable the entire Playerbots module |
| Playerbots.RandomBotAutologin | 1 | Enable the randombot auto-login system |
| Playerbots.MinRandomBots | 500 | Minimum number of randombots to keep online |
| Playerbots.MaxRandomBots | 500 | Maximum number of randombots to keep online |
| Playerbots.RandomBotAccountCount | 0 | Number of randombot accounts (0 = automatic) |
| Playerbots.DeleteRandomBotAccounts | 0 | Set to 1 to delete all randombot accounts on next server start |
| Playerbots.DisabledWithoutRealPlayer | 0 | Disable randombots when no real players are logged in (0 = always active) |
| Playerbots.DisabledWithoutRealPlayerLoginDelay | 30 | Seconds after a real player logs in before randombots begin logging in |
| Playerbots.DisabledWithoutRealPlayerLogoutDelay | 300 | Seconds after last real player logs out before randombots log out |

---

## PLAYERBOTS SETTINGS — GENERAL

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.MaxAddedBots | 40 | Maximum number of bots a player can control simultaneously |
| Playerbots.AddClassCommand | 1 | Enable addclass command (0 = GM only, 1 = all players) |
| Playerbots.AddClassAccountPoolSize | 50 | Number of accounts in the addclass pool |
| Playerbots.GroupInvitationPermission | 1 | Bot group invitation level (0 = GM only, 1 = level-based, 2 = always accept) |
| Playerbots.KeepAltsInGroup | 0 | Keep alt bots in the party even after the master leaves (0 = disabled) |
| Playerbots.BotAutologin | 0 | Auto-login all player alts as altbots when the player logs in |
| Playerbots.AllowAccountBots | 1 | Allow inviting altbots from the player's own account |
| Playerbots.AllowGuildBots | 1 | Allow inviting altbots in the player's guild |
| Playerbots.AllowTrustedAccountBots | 1 | Allow linking accounts for shared altbot control |
| Playerbots.RandomBotGuildNearby | 0 | Randombots will form guilds with nearby randombots |
| Playerbots.RandomBotGuildCount | 20 | Number of guilds created by randombots |
| Playerbots.RandomBotGuildSizeMax | 15 | Maximum members in a randombot guild (minimum is hardcoded to 10) |
| Playerbots.DeleteRandomBotGuilds | 0 | Set to 1 to delete all randombot guilds on next server start |
| Playerbots.RandomBotInvitePlayer | 0 | Randombots will invite real players to groups/raids/guilds |
| Playerbots.InviteChat | 0 | Bots chat in say/guild when inviting other bots |
| Playerbots.SummonWhenGroup | 1 | Bots are automatically summoned to the player when accepting a group invitation |
| Playerbots.SelfBotLevel | 1 | Selfbot permission level (0 = disabled, 1 = GM only, 2 = all players, 3 = auto on login) |
| Playerbots.AutoInitOnly | 0 | Non-GM players restricted to `init=auto` only for bot initialization |
| Playerbots.AutoInitEquipLevelLimitRatio | 1.0 | Upper limit ratio of bot equipment level for init=auto relative to player |
| Playerbots.AllowLearnTrainerSpells | 1 | Allow bots to learn trainer spells when they have the gold |

---

## PLAYERBOTS SETTINGS — SUMMON OPTIONS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.AllowSummonInCombat | 1 | Allow summoning bots while the master is in combat |
| Playerbots.AllowSummonWhenMasterIsDead | 1 | Allow summoning bots when the master is dead |
| Playerbots.AllowSummonWhenBotIsDead | 1 | Allow summoning bots when they are dead (0 = ghosts only, 1 = always) |
| Playerbots.ReviveBotWhenSummoned | 1 | Revive bots when summoning them (0 = never, 1 = out of combat only, 2 = always) |
| Playerbots.BotRepairWhenSummon | 1 | Bots repair their gear when summoned |

---

## PLAYERBOTS SETTINGS — MOUNT

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.UseGroundMountAtMinLevel | 20 | Minimum level for bots to use their 60% ground mount |
| Playerbots.UseFastGroundMountAtMinLevel | 40 | Minimum level for bots to use their 100% fast ground mount |
| Playerbots.UseFlyMountAtMinLevel | 60 | Minimum level for bots to use their 150% flying mount |
| Playerbots.UseFastFlyMountAtMinLevel | 70 | Minimum level for bots to use their 280% fast flying mount |

---

## PLAYERBOTS SETTINGS — GEAR

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.RandomBotShowHelmet | 1 | Show helmet on randombots (requires reset) |
| Playerbots.RandomBotShowCloak | 1 | Show cloak on randombots (requires reset) |
| Playerbots.AutoEquipUpgradeLoot | 1 | Altbots automatically equip looted items that are upgrades |
| Playerbots.EquipUpgradeThreshold | 1.1 | Gear score multiplier required before a looted item is auto-equipped |
| Playerbots.TwoRoundsGearInit | 0 | Run two rounds of gear initialization for more suitable equipment |

---

## PLAYERBOTS SETTINGS — LOOTING

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.FreeMethodLoot | 0 | Bots continue looting when loot method is free-for-all |
| Playerbots.LootNeedRollLevel | 1 | Roll level bots use when they Need (0 = pass, 1 = greed, 2 = need) |
| Playerbots.LootGreedRollLevel | 0 | Enable bots rolling GREED on items globally (0 = disabled, bots only Need or Pass) |
| Playerbots.LootRollRecipe | 0 | Bots roll Need on learnable profession recipes |
| Playerbots.LootRollDisenchant | 0 | Bots with enchanting roll DISENCHANT instead of GREED on disenchantable items |

---

## PLAYERBOTS SETTINGS — TIMERS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.IterationsPerTick | 10 | Max AI iterations per server tick |
| Playerbots.GlobalCooldown | 500 | Delay between two consecutive short-duration spell casts (ms) |
| Playerbots.MaxWaitForMove | 5000 | Max wait time when moving (ms) |
| Playerbots.DisableMoveSplinePath | 0 | Disable MoveSplinePath for bots (0 = enabled, 1 = BG/Arena only, 2 = everywhere) |
| Playerbots.MaxMovementSearchTime | 3 | Max time to search for movement path (higher improves slope navigation) |
| Playerbots.ExpireActionTime | 5000 | Action expiration time (ms) |
| Playerbots.DispelAuraDuration | 700 | Max aura duration to consider for dispel (ms) |
| Playerbots.ReactDelay | 100 | Delay between two bot actions (ms) |
| Playerbots.DynamicReactDelay | 1 | Dynamically adjust react delay by bot status to reduce server lag |
| Playerbots.PassiveDelay | 10000 | Inactivity delay (ms) |
| Playerbots.RepeatDelay | 2000 | Minimum delay between repeating actions like chat or emotes (ms) |
| Playerbots.ErrorDelay | 100 | Delay after an error before retrying (ms) |
| Playerbots.RpgDelay | 10000 | Delay between RPG actions (ms) |
| Playerbots.SitDelay | 20000 | Delay before sitting (ms) |
| Playerbots.ReturnDelay | 2000 | Delay before returning (ms, minimum 2000 — lower values crash) |
| Playerbots.LootDelay | 1000 | Delay before looting (ms) |

---

## PLAYERBOTS SETTINGS — DISTANCES

All distances are in yards.

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.FarDistance | 20.0 | Distance considered "far" for bot positioning |
| Playerbots.SightDistance | 100.0 | Maximum sight range for bots |
| Playerbots.SpellDistance | 28.5 | Maximum distance for casting spells |
| Playerbots.ShootDistance | 5.0 | Minimum distance to start shooting |
| Playerbots.HealDistance | 38.5 | Maximum distance to heal a target |
| Playerbots.LootDistance | 15.0 | Maximum distance to loot |
| Playerbots.FleeDistance | 5.0 | Distance to flee from danger |
| Playerbots.AggroDistance | 22 | Distance at which bots engage enemies |
| Playerbots.TooCloseDistance | 5.0 | Distance considered too close to a target |
| Playerbots.MeleeDistance | 0.75 | Distance for melee attacks |
| Playerbots.FollowDistance | 1.5 | Distance bots maintain while following the master |
| Playerbots.WhisperDistance | 6000.0 | Maximum distance for whisper range |
| Playerbots.ContactDistance | 0.45 | Distance for direct contact interactions |
| Playerbots.AoeRadius | 10 | Radius for AoE spell targeting |
| Playerbots.RpgDistance | 200 | Distance for RPG movement actions |
| Playerbots.GrindDistance | 75.0 | Distance bots search for grind targets |
| Playerbots.ReactDistance | 150.0 | Distance at which bots react to nearby events |

---

## PLAYERBOTS SETTINGS — THRESHOLDS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.CriticalHealth | 25 | Health percentage considered critical |
| Playerbots.LowHealth | 45 | Health percentage considered low |
| Playerbots.MediumHealth | 65 | Health percentage considered medium |
| Playerbots.AlmostFullHealth | 85 | Health percentage considered almost full |
| Playerbots.LowMana | 15 | Mana percentage considered low |
| Playerbots.MediumMana | 40 | Mana percentage considered medium |
| Playerbots.HighMana | 65 | Mana percentage considered high |

---

## PLAYERBOTS SETTINGS — QUESTS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.AutoPickReward | yes | How bots pick quest rewards: `yes` = most useful, `no` = list all, `ask` = useful + list if multiple |
| Playerbots.SyncQuestWithPlayer | 1 | Bots complete quests the moment the player hands them in |
| Playerbots.SyncQuestForPlayer | 0 | Bots auto-complete quests for the player when handing in their own quests |
| Playerbots.DropObsoleteQuests | 1 | Bots automatically drop obsolete quests |

---

## PLAYERBOTS SETTINGS — COMBAT

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.ApplyInstanceStrategies | 1 | Auto-apply dungeon/raid strategies when entering an instance |
| Playerbots.AutoAvoidAoe | 1 | Enable automatic AoE avoidance strategy |
| Playerbots.MaxAoeAvoidRadius | 15.0 | Only avoid AoE spells with a radius smaller than this value (yards) |
| Playerbots.AoeAvoidSpellWhitelist | 50759,57491,13810,29946 | Comma-separated spell IDs that bots should never attempt to avoid |
| Playerbots.AutoSaveMana | 1 | Enable healer bot save-mana strategy |
| Playerbots.SaveManaThreshold | 60 | Mana percentage at which healer bots switch to save-mana mode |
| Playerbots.FleeingEnabled | 1 | Allow bots to flee from enemies (not in the pinned dist) |

---

## PLAYERBOTS SETTINGS — GREATER BUFFS STRATEGIES

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.MinBotsForGreaterBuff | 3 | Minimum group size before Paladins/Mages/Druids use Greater buff variants (not in the pinned dist) |
| Playerbots.RPWarningCooldown | 30 | Seconds between reagent-missing RP warnings, per bot and per buff (not in the pinned dist) |

---

## PLAYERBOTS SETTINGS — CHEATS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.MaintenanceCommand | 1 | Enable the maintenance command (learn spells, assign talents, repair, enchant, etc.) |
| Playerbots.AltMaintenanceAmmo | 1 | Allow maintenance to provide free ammo to alt bots |
| Playerbots.AltMaintenanceFood | 1 | Allow maintenance to provide free food to alt bots |
| Playerbots.AltMaintenanceReagents | 1 | Allow maintenance to provide free reagents to alt bots |
| Playerbots.AltMaintenanceConsumables | 1 | Allow maintenance to provide free consumables to alt bots |
| Playerbots.AltMaintenancePotions | 1 | Allow maintenance to provide free potions to alt bots |
| Playerbots.AltMaintenanceBags | 1 | Allow maintenance to provide free bags to alt bots |
| Playerbots.AltMaintenanceMounts | 1 | Allow maintenance to provide free mounts to alt bots |
| Playerbots.AltMaintenanceSkills | 1 | Allow maintenance to grant free skill levels to alt bots |
| Playerbots.AltMaintenanceClassSpells | 1 | Allow maintenance to grant class spells to alt bots |
| Playerbots.AltMaintenanceAvailableSpells | 1 | Allow maintenance to grant available trainable spells to alt bots |
| Playerbots.AltMaintenanceSpecialSpells | 1 | Allow maintenance to grant special spells (RandomBotSpellIds + DK Death Gate) to alt bots |
| Playerbots.AltMaintenanceTalentTree | 1 | Allow maintenance to assign talent points for alt bots |
| Playerbots.AltMaintenanceGlyphs | 1 | Allow maintenance to apply glyphs to alt bots |
| Playerbots.AltMaintenanceGemsEnchants | 1 | Allow maintenance to socket gems and apply enchants to alt bots |
| Playerbots.AltMaintenancePet | 1 | Allow maintenance to provide free pets to alt bots |
| Playerbots.AltMaintenancePetTalents | 1 | Allow maintenance to assign pet talents for alt bots |
| Playerbots.AltMaintenanceReputation | 1 | Allow maintenance to set reputation for alt bots |
| Playerbots.AltMaintenanceAttunementQuests | 1 | Allow maintenance to complete attunement quests for alt bots |
| Playerbots.AltMaintenanceKeyring | 1 | Allow maintenance to provide keyring items for alt bots |
| Playerbots.AutoGearCommand | 1 | Enable the autogear command to automatically upgrade bot gear |
| Playerbots.AutoGearCommandAltBots | 1 | Enable autogear command for alt bots |
| Playerbots.AutoGearQualityLimit | 3 | Maximum item quality for autogear (1=normal, 2=uncommon, 3=rare, 4=epic, 5=legendary) |
| Playerbots.AutoGearScoreLimit | 0 | Maximum item level for autogear (0 = no limit) |
| Playerbots.BotCheats | "food,taxi,raid" | Comma-separated list of enabled cheats: food, gold, health, mana, power, taxi, raid |
| Playerbots.AttunementQuests | (long list) | Comma-separated quest IDs auto-completed for all bots to bypass attunement requirements |

---

## PLAYERBOTS SETTINGS — FLIGHTPATH

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.BotTaxiDelayMinMs | 350 | Minimum random delay before the first follower bot clicks the flight master (ms) |
| Playerbots.BotTaxiDelayMaxMs | 5000 | Upper bound for the overall taxi delay window, spreads large raids (ms) |
| Playerbots.BotTaxiGapMs | 200 | Fixed gap added per group slot so bots never take off together (ms) |
| Playerbots.BotTaxiGapJitterMs | 100 | Extra random jitter per gap slot to prevent robotic launches (ms) |

---

## PLAYERBOTS SETTINGS — PROFESSIONS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.ClassMatchingProfessionChance | 30 | Percentage of randombots per class bucket that receive a class-matching profession combination |
| Playerbots.EnableFishingWithMaster | 1 | Automatically add the master fishing strategy to fishing bots when they fish with a master |
| Playerbots.FishingDistanceFromMaster | 10.0 | Distance (yards) a bot with a master searches for fishable water |
| Playerbots.FishingDistance | 40.0 | Distance (yards) a masterless bot searches for fishable water (currently unused) |
| Playerbots.EndFishingWithMaster | 30.0 | Distance from water (yards) beyond which a bot removes its master fishing strategy |

---

## RANDOMBOT-SPECIFIC SETTINGS — GENERAL

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.RandomBotRandomPassword | 0 | Use a randomly generated password for randombot accounts |
| Playerbots.RandomBotAccountPrefix | "rndbot" | Prefix for randombot account names |
| Playerbots.RandomBotMinLevel | 1 | Minimum level for randombots |
| Playerbots.RandomBotMaxLevel | 80 | Maximum level for randombots |
| Playerbots.SyncLevelWithPlayers | 0 | Sync max randombot level with the max level of currently online players |
| Playerbots.PreQuests | 0 | Mark many quests at or below bot level as complete on bot creation (slows creation) |
| Playerbots.RandomBotJoinLfg | 1 | Enable LFG queue participation for randombots |
| Playerbots.EnablePeriodicOnlineOffline | 0 | Enable randombots periodically going online/offline to simulate player patterns |
| Playerbots.PeriodicOnlineOfflineRatio | 2.0 | Ratio of total bots (including offline) to MaxRandomBots; only applies when EnablePeriodicOnlineOffline is on |
| Playerbots.RandomBotAllianceRatio | 50 | Percentage of randombots that are Alliance |
| Playerbots.RandomBotHordeRatio | 50 | Percentage of randombots that are Horde |
| Playerbots.DisableDeathKnightLogin | 0 | Prevent Death Knight randombots from logging in |
| Playerbots.LimitTalentsExpansion | 0 | Simulate expansion-limited talent trees based on bot level |
| Playerbots.EnableRandomBotTrading | 1 | Configure randombot trading (0=off, 1=both, 2=buy only, 3=sell only) |
| Playerbots.TradeActionExcludedPrefixes | (addon list) | Message prefixes excluded from trade-window trigger analysis |

---

## RANDOMBOT-SPECIFIC SETTINGS — LEVELS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.DisableRandomLevels | 0 | Disable random level generation; all bots start at RandombotStartingLevel |
| Playerbots.RandombotStartingLevel | 1 | Starting level for randombots when DisableRandomLevels is enabled |
| Playerbots.RandomBotMinLevelChance | 0.1 | Probability (0–1) a randombot starts at the minimum level on first randomize |
| Playerbots.RandomBotMaxLevelChance | 0.1 | Probability (0–1) a randombot starts at the maximum level on first randomize |
| Playerbots.RandomBotFixedLevel | 0 | Freeze bot levels so they cannot level up |
| Playerbots.DowngradeMaxLevelBot | 0 | Reset max-level bots back to RandomBotMinLevel |
| Playerbots.RandomBotXPRate | 1.0 | XP rate multiplier for randombots (multiplied by server rate) |

---

## RANDOMBOT-SPECIFIC SETTINGS — GEAR

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.RandomGearQualityLimit | 3 | Maximum gear quality for randomly generated randombot equipment (1–5) |
| Playerbots.RandomGearScoreLimit | 0 | Maximum item level for randombot gear (0 = no limit) |
| Playerbots.PreferClassArmorType | 0 | Apply 3x score multiplier to class-appropriate armor type during gear selection |
| Playerbots.PreferredSpecWeapons | 0 | Prefer spec-appropriate weapon types during autogear (e.g., slow 2H for Arms Warriors) |
| Playerbots.IncrementalGearInit | 1 | Randombots upgrade gear incrementally through loot and quests (0 = loot/quest only) (not in the pinned dist) |
| Playerbots.MinEnchantingBotLevel | 60 | Minimum bot level to apply enchants and socket gems via maintenance |
| Playerbots.LimitEnchantExpansion | 1 | Bots do not use TBC enchants before level 61 or WotLK enchants/gems before level 71 |
| Playerbots.LimitGearExpansion | 1 | Bots do not equip TBC gear before level 61 or WotLK gear before level 71 |
| Playerbots.RandomGearLoweringChance | 0 | Chance (0–1) a randombot receives suboptimal gear on initialization |
| Playerbots.UnobtainableItems | 12468,46978 | Comma-separated item IDs excluded from bot gear generation (default changed: now `12468,44869,44870,46978`) |
| Playerbots.GearScoreCheck | 0 | Randombots deny group invitations from players with too-low gear score |
| Playerbots.EquipmentPersistence | 0 | Stop random gear re-initialization after EquipmentPersistenceLevel (not in the pinned dist; superseded by `Playerbots.EquipAndSpecPersistence`, default 1) |
| Playerbots.EquipmentPersistenceLevel | 80 | Level at which equipment persistence kicks in (not in the pinned dist; superseded by `Playerbots.EquipAndSpecPersistenceLevel`, default 1) |
| Playerbots.AutoUpgradeEquip | 1 | Randombots automatically upgrade equipment on level-up |
| Playerbots.HunterWolfPet | 0 | Force wolf pets for hunters (0=disabled, 1=max-level only, 2=always) |
| Playerbots.DefaultPetStance | 1 | Default pet stance on summon (0=Passive, 1=Defensive, 2=Aggressive) |
| Playerbots.PetChatCommandDebug | 0 | Enable debug messages for pet commands |
| Playerbots.ExcludedHunterPetFamilies | "" | Comma-separated creature family IDs prohibited as hunter bot pets |

---

## RANDOMBOT-SPECIFIC SETTINGS — ACTIVITY

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.BotActiveAlone | 10 | Approximate percentage of bots that are active when no real players are nearby |
| Playerbots.BotActiveAloneDurationSeconds | 30 | How often (seconds) the active bot roster rotates |
| Playerbots.BotActiveAloneForceWhenInRadius | 150 | Force-activate bots within this many yards of a real player (0 = disabled) |
| Playerbots.BotActiveAloneForceWhenInZone | 1 | Force-activate bots in the same zone as a real player |
| Playerbots.BotActiveAloneForceWhenInMap | 0 | Force-activate bots on the same continent as a real player |
| Playerbots.BotActiveAloneForceWhenIsFriend | 0 | Force-activate bots on a real player's friends list |
| Playerbots.BotActiveAloneForceWhenInGuild | 1 | Force-activate bots in a guild that has at least one real player |
| Playerbots.botActiveAloneSmartScale | 1 | Automatically reduce active bots when server tick time is high |
| Playerbots.botActiveAloneSmartScaleDiffLimitfloor | 50 | Server update time (ms) below which no SmartScale reduction occurs |
| Playerbots.botActiveAloneSmartScaleDiffLimitCeiling | 200 | Server update time (ms) at which all non-forced bots are paused |
| Playerbots.botActiveAloneSmartScaleWhenMinLevel | 1 | Minimum bot level affected by SmartScale |
| Playerbots.botActiveAloneSmartScaleWhenMaxLevel | 80 | Maximum bot level affected by SmartScale |

---

## RANDOMBOT-SPECIFIC SETTINGS — QUESTS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.RandomBotQuestIds | (long list) | Quest IDs automatically completed and rewarded for all randombots |
| Playerbots.RandomBotGroupNearby | 0 | Randombots will group with nearby randombots to complete shared quests |
| Playerbots.AutoDoQuests | 1 | Randombots pick up quests on their own and attempt to complete them |
| Playerbots.RandomBotQuestItems | (item ID list) | Item IDs that bots will not destroy from their inventories |

---

## RANDOMBOT-SPECIFIC SETTINGS — SPELLS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.AutoLearnQuestSpells | 0 | Randombots automatically learn class quest reward spells on level-up (default changed: now 1) |
| Playerbots.AutoLearnTrainerSpells | 1 | Randombots automatically learn trainable spells on level-up |
| Playerbots.AutoPickTalents | 1 | Randombots automatically spend talent points on level-up |
| Playerbots.RandomBotSpellIds | "54197" | Comma-separated spell IDs every randombot learns automatically (default: Cold Weather Flying) |
| Playerbots.OpenGoSpell | 6477 | Spell ID used to open lootable chests |

---

## RANDOMBOT-SPECIFIC SETTINGS — STRATEGIES

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.RandomBotCombatStrategies | "" | Additional combat strategies applied to all randombots (e.g., `"+threat,-potions"`) |
| Playerbots.RandomBotNonCombatStrategies | "" | Additional non-combat strategies applied to all randombots |
| Playerbots.CombatStrategies | "" | Additional combat strategies applied to all altbots |
| Playerbots.NonCombatStrategies | "" | Additional non-combat strategies applied to all altbots |
| Playerbots.HealerDPSMapRestriction | 1 | Remove "healer dps" strategy on specified maps |
| Playerbots.RestrictedHealerDPSMaps | (dungeon/raid map ID list) | Map IDs where healer DPS strategy is removed when HealerDPSMapRestriction is enabled |

---

## RANDOMBOT-SPECIFIC SETTINGS — RPG STRATEGY

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.EnableNewRpgStrategy | 1 | Enable the RPG strategy; overrides AutoDoQuests, RandomBotTeleLowerLevel, RandomBotTeleHigherLevel |
| Playerbots.RpgStatusProbWeight.WanderRandom | 15 | Weight for "wander randomly to find and kill mobs" RPG status |
| Playerbots.RpgStatusProbWeight.WanderNpc | 20 | Weight for "randomly interact with nearby NPCs" RPG status |
| Playerbots.RpgStatusProbWeight.GoGrind | 15 | Weight for "go to level-appropriate area to grind" RPG status |
| Playerbots.RpgStatusProbWeight.GoCamp | 10 | Weight for "return to nearby camp near innkeeper/flightmaster" RPG status |
| Playerbots.RpgStatusProbWeight.DoQuest | 60 | Weight for "select and head to quest objective" RPG status |
| Playerbots.RpgStatusProbWeight.TravelFlight | 15 | Weight for "fly to level-appropriate area" RPG status |
| Playerbots.RpgStatusProbWeight.Rest | 5 | Weight for "do nothing for a while" RPG status |
| Playerbots.RpgStatusProbWeight.OutdoorPvp | 10 | Weight for "participate in outdoor PvP capture points" RPG status |
| Playerbots.RpgStatusProbWeight.DoGather | 15 | Weight for "roam the current zone harvesting herb/ore nodes" RPG status (requires Herbalism or Mining) |

### ZoneBracket entries

`Playerbots.ZoneBracket.<zoneId> = minLevel,maxLevel` sets the level range for randombots using the RPG strategy in each zone. Bots teleport out of a zone if their level falls outside the bracket. Format: `"min,max"`.

Examples:
- `Playerbots.ZoneBracket.12 = 5,12` — Elwynn Forest, levels 5–12
- `Playerbots.ZoneBracket.3483 = 58,66` — Hellfire Peninsula, levels 58–66
- `Playerbots.ZoneBracket.3537 = 68,75` — Borean Tundra, levels 68–75
- `Playerbots.ZoneBracket.66 = 74,80` — Zul'Drak, levels 74–80

See `docs/configs/playerbots.conf.dist` for the full list covering all Classic, TBC, and WotLK zones (~40 entries total).

---

## RANDOMBOT-SPECIFIC SETTINGS — TELEPORTS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.RandomBotMaps | 0,1,530,571 | Map IDs bots may teleport to (EK, Kalimdor, Outland, Northrend) |
| Playerbots.ProbTeleToBankers | 0.25 | Probability a bot teleports to a city banker |
| Playerbots.EnableWeightTeleToCityBankers | 1 | Use weighted city selection for banker teleports |
| Playerbots.TeleToStormwindWeight | 2 | Weight for teleporting to Stormwind |
| Playerbots.TeleToIronforgeWeight | 1 | Weight for teleporting to Ironforge |
| Playerbots.TeleToDarnassusWeight | 1 | Weight for teleporting to Darnassus |
| Playerbots.TeleToExodarWeight | 1 | Weight for teleporting to the Exodar |
| Playerbots.TeleToOrgrimmarWeight | 2 | Weight for teleporting to Orgrimmar |
| Playerbots.TeleToUndercityWeight | 1 | Weight for teleporting to Undercity |
| Playerbots.TeleToThunderBluffWeight | 1 | Weight for teleporting to Thunder Bluff |
| Playerbots.TeleToSilvermoonCityWeight | 1 | Weight for teleporting to Silvermoon City |
| Playerbots.TeleToShattrathCityWeight | 1 | Weight for teleporting to Shattrath City |
| Playerbots.TeleToDalaranWeight | 1 | Weight for teleporting to Dalaran |
| Playerbots.RandomBotTeleportDistance | 100 | Distance (yards) randombots teleport after death |
| Playerbots.RandomBotTeleLowerLevel | 1 | Levels below zone minimum a bot can be before teleporting out (no effect when NewRpgStrategy enabled) |
| Playerbots.RandomBotTeleHigherLevel | 3 | Levels above zone maximum a bot can be before teleporting out (no effect when NewRpgStrategy enabled) |
| Playerbots.AutoTeleportForLevel | 1 | Bots automatically teleport to a new leveling area on level-up |

---

## RANDOMBOT-SPECIFIC SETTINGS — BATTLEGROUND & ARENA & PVP

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.RandomBotJoinBG | 1 | Enable battleground and arena participation for randombots |
| Playerbots.RandomBotAutoJoinBG | 0 | Bots auto-start BGs and arenas on their own |
| Playerbots.RandomBotAutoJoinICBrackets | 1 | Isle of Conquest bracket(s) for auto-join |
| Playerbots.RandomBotAutoJoinEYBrackets | 2 | Eye of the Storm bracket(s) for auto-join |
| Playerbots.RandomBotAutoJoinAVBrackets | 3 | Alterac Valley bracket(s) for auto-join |
| Playerbots.RandomBotAutoJoinABBrackets | 6 | Arathi Basin bracket(s) for auto-join |
| Playerbots.RandomBotAutoJoinWSBrackets | 7 | Warsong Gulch bracket(s) for auto-join |
| Playerbots.RandomBotAutoJoinBGICCount | 0 | Number of IoC instances per bracket |
| Playerbots.RandomBotAutoJoinBGEYCount | 1 | Number of EotS instances per bracket |
| Playerbots.RandomBotAutoJoinBGAVCount | 0 | Number of AV instances per bracket |
| Playerbots.RandomBotAutoJoinBGABCount | 1 | Number of AB instances per bracket |
| Playerbots.RandomBotAutoJoinBGWSCount | 1 | Number of WSG instances per bracket |
| Playerbots.RandomBotAutoJoinArenaBracket | 14 | Rated arena bracket for auto-join (14 = level 80) |
| Playerbots.RandomBotAutoJoinBGRatedArena2v2Count | 0 | Number of rated 2v2 arenas to fill per bracket |
| Playerbots.RandomBotAutoJoinBGRatedArena3v3Count | 0 | Number of rated 3v3 arenas to fill per bracket |
| Playerbots.RandomBotAutoJoinBGRatedArena5v5Count | 0 | Number of rated 5v5 arenas to fill per bracket |
| Playerbots.RandomBotArenaTeam2v2Count | 10 | Number of 2v2 bot arena teams created on server start |
| Playerbots.RandomBotArenaTeam3v3Count | 10 | Number of 3v3 bot arena teams created on server start |
| Playerbots.RandomBotArenaTeam5v5Count | 5 | Number of 5v5 bot arena teams created on server start |
| Playerbots.RandomBotArenaTeamMaxRating | 2000 | Maximum randomized rating for new bot arena teams |
| Playerbots.RandomBotArenaTeamMinRating | 1000 | Minimum randomized rating for new bot arena teams |
| Playerbots.DeleteRandomBotArenaTeams | 0 | Set to 1 to delete all randombot arena teams on next server start |
| Playerbots.PvpProhibitedZoneIds | (long list) | Zone IDs where bots will not engage in PvP |
| Playerbots.PvpProhibitedAreaIds | (long list) | Area IDs where bots will not engage in PvP |
| Playerbots.FastReactInBG | 1 | Improve bot reaction speed in battlegrounds and arenas (may cause lag) |

---

## RANDOMBOT-SPECIFIC SETTINGS — RANDOM BOT TIMING AND BEHAVIOR

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.RandomBotUpdateInterval | 20 | Seconds between random bot manager main update loop runs |
| Playerbots.RandomBotCountChangeMinInterval | 1800 | Minimum seconds before manager re-evaluates total bot count |
| Playerbots.RandomBotCountChangeMaxInterval | 7200 | Maximum seconds before manager re-evaluates total bot count |
| Playerbots.MinRandomBotInWorldTime | 600 | Minimum seconds a randombot stays online before logging out |
| Playerbots.MaxRandomBotInWorldTime | 28800 | Maximum seconds a randombot stays online before logging out |
| Playerbots.MinRandomBotRandomizeTime | 7200 | Minimum seconds before a bot is eligible for re-randomization |
| Playerbots.MaxRandomBotRandomizeTime | 1209600 | Maximum seconds before a bot is eligible for re-randomization |
| Playerbots.RandomBotsPerInterval | 60 | Number of bots processed (login/logout/update) per manager update cycle |
| Playerbots.MinRandomBotReviveTime | 60 | Minimum seconds after death before a bot revives |
| Playerbots.MaxRandomBotReviveTime | 300 | Maximum seconds after death before a bot revives |
| Playerbots.MinRandomBotTeleportInterval | 3600 | Minimum seconds between bot teleports to new areas |
| Playerbots.MaxRandomBotTeleportInterval | 18000 | Maximum seconds between bot teleports to new areas |
| Playerbots.PermanentlyInWorldTime | 31104000 | Seconds a "permanent" bot stays in the world (~1 year) |

---

## PREMADE SPECS

Premade spec entries define the talent builds, glyphs, and gear progression used by bots. They use three key families:

- `Playerbots.PremadeSpecName.<classId>.<specIndex>` — human-readable name for the spec (e.g., `"arms pve"`)
- `Playerbots.PremadeSpecGlyph.<classId>.<specIndex>` — comma-separated glyph item IDs in order: major1, minor1, major2, minor2, minor3, major3
- `Playerbots.PremadeSpecLink.<classId>.<specIndex>.<level>` — Wowhead-style talent link string the bot works toward at the given level

Class IDs: 1=Warrior, 2=Paladin, 3=Hunter, 4=Rogue, 5=Priest, 6=Death Knight, 7=Shaman, 8=Mage, 9=Warlock, 11=Druid. Spec indexes start at 0. Level breakpoints are typically 40, 60, 65, 70, 80 (not all are defined for every spec).

Hunter bots also support `Playerbots.PremadeHunterPetLink.<petType>.<level>` for pet talent builds (0=Ferocity, 1=Tenacity, 2=Cunning).

To customize bot talent builds, edit the appropriate PremadeSpecLink entry for the desired class, spec, and level. Build the talent string at https://www.wowhead.com/wotlk/talent-calc. See `docs/configs/playerbots.conf.dist` for all entries (Warrior specs 0–5, Paladin 0–5, Hunter 0–5, Rogue 0–5, Priest 0–5, DK 0–6, Shaman 0–5, Mage 0–6, Warlock 0–5, Druid 0–6).

---

## WORLD BUFFS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.WorldBuffMatrix | (large matrix) | Defines auto-refreshing world buffs applied to bots by faction, class, spec, and level range. Format: `Entry:FactionID,ClassID,SpecID,MinLevel,MaxLevel:SpellID1,SpellID2,...;`. FactionID 0 = both factions. Requires `nc +worldbuff` command sent to the bot to activate. |

The default matrix applies level-appropriate Vanilla (60–69), TBC (70–79), and WotLK (80) world buff simulations (flasks, food, runes) for every implemented PvE spec. Custom entries can be added or existing ones modified.

---

## RANDOMBOT DEFAULT TALENT SPECS

These control which PremadeSpec index each randombot class picks and with what probability.

`Playerbots.RandomClassSpecProb.<classId>.<specIndex>` — probability weight for selecting this spec (weights need not sum to 100; set to 0 to disable).
`Playerbots.RandomClassSpecIndex.<classId>.<specIndex>` — which PremadeSpec index to use when this spec is selected.

Default distributions (PvP specs are all 0 — disabled by default):

| Class | Specs and default weights |
|-------|--------------------------|
| Warrior (1) | arms pve=20, fury pve=40, prot pve=40 |
| Paladin (2) | holy pve=30, prot pve=40, ret pve=30 |
| Hunter (3) | bm pve=33, mm pve=33, surv pve=33 |
| Rogue (4) | as pve=45, combat pve=45, subtlety pve=10 |
| Priest (5) | disc pve=40, holy pve=35, shadow pve=25 |
| Death Knight (6) | blood pve=30, frost pve=40, unholy pve=30 |
| Shaman (7) | ele pve=33, enh pve=33, resto pve=33 |
| Mage (8) | arcane pve=30, fire pve=30, frost pve=40 |
| Warlock (9) | affli pve=33, demo pve=34, destro pve=33 |
| Druid (11) | balance pve=20, bear pve=25, resto pve=35, cat pve=20 |

---

## PLAYERBOTS SYSTEM SETTINGS — DATABASE & CONNECTIONS

| Key | Default | Description |
|-----|---------|-------------|
| PlayerbotsDatabaseInfo | "127.0.0.1;3306;acore;acore;acore_playerbots" | Database connection string for the playerbots database |
| PlayerbotsDatabase.WorkerThreads | 1 | Number of async worker threads for MySQL statements |
| PlayerbotsDatabase.SynchThreads | 1 | Number of synchronous MySQL connections |
| Playerbots.Updates.EnableDatabases | 1 | Enable the DB update system for the playerbots database |
| Playerbots.CommandServerPort | 8888 | TCP port for the command server (0 = disabled) |

---

## PLAYERBOTS SYSTEM SETTINGS — DEBUG

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.SpellDump | 0 | Dump spell information for debugging |
| Playerbots.LogInGroupOnly | 1 | Only log bot actions when in a group |
| Playerbots.LogValuesPerTick | 0 | Log bot values every tick |
| Playerbots.RandomChangeMultiplier | 1 | Multiplier for random changes |
| Playerbots.TellWhenAvoidAoe | 0 | Bot tells which spell it is avoiding (experimental) |
| Playerbots.PerfMonEnabled | 0 | Enable the performance monitor |

---

## PLAYERBOTS SYSTEM SETTINGS — CHAT SETTINGS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.CommandPrefix | "" | Prefix required before bot chat commands |
| Playerbots.CommandSeparator | "\\\\" | Separator character between chained bot commands |
| Playerbots.EnableAutoTradeOnItemMention | 1 | Automatically show inventory and open trade when item keywords are mentioned in chat |
| Playerbots.RandomBotTalk | 1 | Enable bots talking in say/yell/general/LFG channels |
| Playerbots.RandomBotEmote | 0 | Enable bots performing emotes |
| Playerbots.RandomBotSuggestDungeons | 1 | Randombots suggest dungeons in chat |
| Playerbots.EnableGreet | 0 | Bots greet players when invited to a group |
| Playerbots.ToxicLinksRepliesChance | 30 | Chance (0–100) bots reply to toxic links with their own toxic links |
| Playerbots.ThunderfuryRepliesChance | 40 | Chance (0–100) bots reply to Thunderfury mentions |
| Playerbots.GuildFeedback | 1 | Bots chat in guild about certain in-game events (not in the pinned dist; the old key was spelled `AIPlayerbot.GuildFeedback`) |
| Playerbots.GuildRepliesRate | 100 | Chance (0–100) bots reply in guild chat about events |
| Playerbots.RandomBotSayWithoutMaster | 0 | Masterless bots say their idle lines |

---

## PLAYERBOTS SYSTEM SETTINGS — BROADCAST RATES

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.EnableBroadcasts | 1 | Enable or disable all broadcasts globally |
| Playerbots.BroadcastToGuildGlobalChance | 30000 | Global chance for guild channel broadcasts (0–30000) |
| Playerbots.BroadcastToWorldGlobalChance | 30000 | Global chance for world channel broadcasts |
| Playerbots.BroadcastToGeneralGlobalChance | 30000 | Global chance for general channel broadcasts |
| Playerbots.BroadcastToTradeGlobalChance | 30000 | Global chance for trade channel broadcasts |
| Playerbots.BroadcastToLFGGlobalChance | 30000 | Global chance for LFG channel broadcasts |
| Playerbots.BroadcastToLocalDefenseGlobalChance | 30000 | Global chance for local defense channel broadcasts |
| Playerbots.BroadcastToWorldDefenseGlobalChance | 30000 | Global chance for world defense channel broadcasts |
| Playerbots.BroadcastToGuildRecruitmentGlobalChance | 30000 | Global chance for guild recruitment channel broadcasts |
| Playerbots.BroadcastChanceLootingItemPoor | 30 | Chance bot broadcasts looting a poor-quality item |
| Playerbots.BroadcastChanceLootingItemNormal | 150 | Chance bot broadcasts looting a normal-quality item |
| Playerbots.BroadcastChanceLootingItemUncommon | 10000 | Chance bot broadcasts looting an uncommon item |
| Playerbots.BroadcastChanceLootingItemRare | 20000 | Chance bot broadcasts looting a rare item |
| Playerbots.BroadcastChanceLootingItemEpic | 30000 | Chance bot broadcasts looting an epic item |
| Playerbots.BroadcastChanceLootingItemLegendary | 30000 | Chance bot broadcasts looting a legendary item |
| Playerbots.BroadcastChanceLootingItemArtifact | 30000 | Chance bot broadcasts looting an artifact item |
| Playerbots.BroadcastChanceQuestAccepted | 6000 | Chance bot broadcasts accepting a quest |
| Playerbots.BroadcastChanceQuestUpdateObjectiveCompleted | 300 | Chance bot broadcasts completing a quest objective |
| Playerbots.BroadcastChanceQuestUpdateObjectiveProgress | 300 | Chance bot broadcasts quest objective progress |
| Playerbots.BroadcastChanceQuestUpdateFailedTimer | 300 | Chance bot broadcasts a failed quest timer |
| Playerbots.BroadcastChanceQuestUpdateComplete | 1000 | Chance bot broadcasts completing a quest |
| Playerbots.BroadcastChanceQuestTurnedIn | 10000 | Chance bot broadcasts turning in a quest |
| Playerbots.BroadcastChanceKillNormal | 30 | Chance bot broadcasts killing a normal mob |
| Playerbots.BroadcastChanceKillElite | 300 | Chance bot broadcasts killing an elite mob |
| Playerbots.BroadcastChanceKillRareelite | 3000 | Chance bot broadcasts killing a rare elite |
| Playerbots.BroadcastChanceKillWorldboss | 20000 | Chance bot broadcasts killing a world boss |
| Playerbots.BroadcastChanceKillRare | 10000 | Chance bot broadcasts killing a rare mob |
| Playerbots.BroadcastChanceKillUnknown | 100 | Chance bot broadcasts killing an unknown mob type |
| Playerbots.BroadcastChanceKillPet | 10 | Chance bot broadcasts killing a pet |
| Playerbots.BroadcastChanceKillPlayer | 30 | Chance bot broadcasts killing a player |
| Playerbots.BroadcastChanceLevelupGeneric | 20000 | Chance bot broadcasts a generic level-up |
| Playerbots.BroadcastChanceLevelupTenX | 30000 | Chance bot broadcasts a level that is a multiple of 10 |
| Playerbots.BroadcastChanceLevelupMaxLevel | 30000 | Chance bot broadcasts reaching max level |
| Playerbots.BroadcastChanceSuggestInstance | 5000 | Chance bot broadcasts a dungeon/raid suggestion |
| Playerbots.BroadcastChanceSuggestQuest | 10000 | Chance bot broadcasts a quest suggestion |
| Playerbots.BroadcastChanceSuggestGrindMaterials | 5000 | Chance bot broadcasts a grind materials suggestion |
| Playerbots.BroadcastChanceSuggestGrindReputation | 5000 | Chance bot broadcasts a reputation grind suggestion |
| Playerbots.BroadcastChanceSuggestSell | 300 | Chance bot broadcasts a selling suggestion |
| Playerbots.BroadcastChanceSuggestSomething | 30000 | Chance bot broadcasts a generic suggestion |
| Playerbots.BroadcastChanceSuggestSomethingToxic | 0 | Chance bot says rude things (disabled by default) |
| Playerbots.BroadcastChanceSuggestToxicLinks | 0 | Chance bot says `"<word> [item link]"` toxic lines (disabled by default) |
| Playerbots.ToxicLinksPrefix | gnomes | Word used as prefix in toxic item link broadcasts |
| Playerbots.BroadcastChanceSuggestThunderfury | 1 | Chance bot suggests Thunderfury |
| Playerbots.BroadcastChanceGuildManagement | 30000 | Chance bot broadcasts guild management messages (ignores global chance) |

---

## PLAYERBOTS SYSTEM SETTINGS — LOGS

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.AllowedLogFiles | "" | Comma-separated list of log filenames bots are allowed to create |
| Playerbots.DisallowedGameObjects | (long GUID list) | Comma-separated game object GUIDs bots are not allowed to interact with |

---

## DEPRECATED SETTINGS (still in use)

| Key | Default | Description |
|-----|---------|-------------|
| Playerbots.RandomBotLoginAtStartup | 1 | Log on all randombots at server startup (not in the pinned dist) |
| Playerbots.EnableGuildTasks | 0 | Enable the guild task system |
| Playerbots.SuggestDungeonsInLowerCaseRandomly | 0 | Randomly suggest dungeons in lower case (not in the pinned dist) |
| Playerbots.RandomBotRpgChance | 0.20 | Chance bot teleports to random camp for level instead of grinding (legacy RPG strategy) |
| Playerbots.RandombotsWalkingRPG | 0 | Force randombots to walk everywhere |
| Playerbots.RandombotsWalkingRPG.InDoors | 0 | Force randombots to walk inside buildings only |
| Playerbots.PremadeAvoidAoe | 62234-4 | Premade AoE spell avoid entries for undetected spells (spellid-radius format) (not in the pinned dist) |
| Playerbots.MinRandomBotsPriceChangeInterval | 7200 | Minimum seconds between randombot AH price changes |
| Playerbots.MaxRandomBotsPriceChangeInterval | 172800 | Maximum seconds between randombot AH price changes |
| Playerbots.MinRandomBotChangeStrategyTime | 180 | Minimum seconds before bots change strategy |
| Playerbots.MaxRandomBotChangeStrategyTime | 720 | Maximum seconds before bots change strategy |
| Playerbots.MinGuildTaskChangeTime | 172800 | Minimum seconds between guild task changes |
| Playerbots.MaxGuildTaskChangeTime | 432000 | Maximum seconds between guild task changes |
| Playerbots.MinGuildTaskAdvertisementTime | 300 | Minimum seconds between guild task mail advertisements |
| Playerbots.MaxGuildTaskAdvertisementTime | 28800 | Maximum seconds between guild task mail advertisements |
| Playerbots.MinGuildTaskRewardTime | 300 | Minimum seconds before task reward mail is sent |
| Playerbots.MaxGuildTaskRewardTime | 3600 | Maximum seconds before task reward mail is sent |
| Playerbots.GuildTaskAdvertCleanupTime | 300 | Seconds between guild task advertisement cleanup runs |
| Playerbots.GuildTaskKillTaskDistance | 200 | Maximum distance between victim and bot when creating a guild kill task |
| Playerbots.TargetPosRecalcDistance | 0.1 | Distance margin for facade (positioning) calculations |
| Playerbots.SummonAtInnkeepersEnabled | 1 | Allow bots to be summoned near innkeepers |
| Playerbots.EnableICCBuffs | 1 | Apply ICC helper buffs on PP, Sindragosa, and Lich King to make Heroic mode more accessible |
