# playerbots.conf Reference
> Source of truth: `docs/configs/playerbots.conf.dist`
> Set these via AC_* env vars in `docker-compose.override.yml` or edit configs/modules/playerbots.conf directly.
> Note: env vars override conf values; the AC_* derivation rule applies (see ref-config-worldserver.md).

---

## GENERAL SETTINGS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.Enabled | 1 | Enable or disable the entire Playerbots module |
| AiPlayerbot.RandomBotAutologin | 1 | Enable the randombot auto-login system |
| AiPlayerbot.MinRandomBots | 500 | Minimum number of randombots to keep online |
| AiPlayerbot.MaxRandomBots | 500 | Maximum number of randombots to keep online |
| AiPlayerbot.RandomBotAccountCount | 0 | Number of randombot accounts (0 = automatic) |
| AiPlayerbot.DeleteRandomBotAccounts | 0 | Set to 1 to delete all randombot accounts on next server start |
| AiPlayerbot.DisabledWithoutRealPlayer | 0 | Disable randombots when no real players are logged in (0 = always active) |
| AiPlayerbot.DisabledWithoutRealPlayerLoginDelay | 30 | Seconds after a real player logs in before randombots begin logging in |
| AiPlayerbot.DisabledWithoutRealPlayerLogoutDelay | 300 | Seconds after last real player logs out before randombots log out |

---

## PLAYERBOTS SETTINGS — GENERAL

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.MaxAddedBots | 40 | Maximum number of bots a player can control simultaneously |
| AiPlayerbot.AddClassCommand | 1 | Enable addclass command (0 = GM only, 1 = all players) |
| AiPlayerbot.AddClassAccountPoolSize | 50 | Number of accounts in the addclass pool |
| AiPlayerbot.GroupInvitationPermission | 1 | Bot group invitation level (0 = GM only, 1 = level-based, 2 = always accept) |
| AiPlayerbot.KeepAltsInGroup | 0 | Keep alt bots in the party even after the master leaves (0 = disabled) |
| AiPlayerbot.BotAutologin | 0 | Auto-login all player alts as altbots when the player logs in |
| AiPlayerbot.AllowAccountBots | 1 | Allow inviting altbots from the player's own account |
| AiPlayerbot.AllowGuildBots | 1 | Allow inviting altbots in the player's guild |
| AiPlayerbot.AllowTrustedAccountBots | 1 | Allow linking accounts for shared altbot control |
| AiPlayerbot.RandomBotGuildNearby | 0 | Randombots will form guilds with nearby randombots |
| AiPlayerbot.RandomBotGuildCount | 20 | Number of guilds created by randombots |
| AiPlayerbot.RandomBotGuildSizeMax | 15 | Maximum members in a randombot guild (minimum is hardcoded to 10) |
| AiPlayerbot.DeleteRandomBotGuilds | 0 | Set to 1 to delete all randombot guilds on next server start |
| AiPlayerbot.RandomBotInvitePlayer | 0 | Randombots will invite real players to groups/raids/guilds |
| AiPlayerbot.InviteChat | 0 | Bots chat in say/guild when inviting other bots |
| AiPlayerbot.SummonWhenGroup | 1 | Bots are automatically summoned to the player when accepting a group invitation |
| AiPlayerbot.SelfBotLevel | 1 | Selfbot permission level (0 = disabled, 1 = GM only, 2 = all players, 3 = auto on login) |
| AiPlayerbot.AutoInitOnly | 0 | Non-GM players restricted to `init=auto` only for bot initialization |
| AiPlayerbot.AutoInitEquipLevelLimitRatio | 1.0 | Upper limit ratio of bot equipment level for init=auto relative to player |
| AiPlayerbot.AllowLearnTrainerSpells | 1 | Allow bots to learn trainer spells when they have the gold |

---

## PLAYERBOTS SETTINGS — SUMMON OPTIONS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.AllowSummonInCombat | 1 | Allow summoning bots while the master is in combat |
| AiPlayerbot.AllowSummonWhenMasterIsDead | 1 | Allow summoning bots when the master is dead |
| AiPlayerbot.AllowSummonWhenBotIsDead | 1 | Allow summoning bots when they are dead (0 = ghosts only, 1 = always) |
| AiPlayerbot.ReviveBotWhenSummoned | 1 | Revive bots when summoning them (0 = never, 1 = out of combat only, 2 = always) |
| AiPlayerbot.BotRepairWhenSummon | 1 | Bots repair their gear when summoned |

---

## PLAYERBOTS SETTINGS — MOUNT

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.UseGroundMountAtMinLevel | 20 | Minimum level for bots to use their 60% ground mount |
| AiPlayerbot.UseFastGroundMountAtMinLevel | 40 | Minimum level for bots to use their 100% fast ground mount |
| AiPlayerbot.UseFlyMountAtMinLevel | 60 | Minimum level for bots to use their 150% flying mount |
| AiPlayerbot.UseFastFlyMountAtMinLevel | 70 | Minimum level for bots to use their 280% fast flying mount |

---

## PLAYERBOTS SETTINGS — GEAR

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.RandomBotShowHelmet | 1 | Show helmet on randombots (requires reset) |
| AiPlayerbot.RandomBotShowCloak | 1 | Show cloak on randombots (requires reset) |
| AiPlayerbot.AutoEquipUpgradeLoot | 1 | Altbots automatically equip looted items that are upgrades |
| AiPlayerbot.EquipUpgradeThreshold | 1.1 | Gear score multiplier required before a looted item is auto-equipped |
| AiPlayerbot.TwoRoundsGearInit | 0 | Run two rounds of gear initialization for more suitable equipment |

---

## PLAYERBOTS SETTINGS — LOOTING

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.FreeMethodLoot | 0 | Bots continue looting when loot method is free-for-all |
| AiPlayerbot.LootNeedRollLevel | 1 | Roll level bots use when they Need (0 = pass, 1 = greed, 2 = need) |
| AiPlayerbot.LootGreedRollLevel | 0 | Enable bots rolling GREED on items globally (0 = disabled, bots only Need or Pass) |
| AiPlayerbot.LootRollRecipe | 0 | Bots roll Need on learnable profession recipes |
| AiPlayerbot.LootRollDisenchant | 0 | Bots with enchanting roll DISENCHANT instead of GREED on disenchantable items |

---

## PLAYERBOTS SETTINGS — TIMERS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.IterationsPerTick | 10 | Max AI iterations per server tick |
| AiPlayerbot.GlobalCooldown | 500 | Delay between two consecutive short-duration spell casts (ms) |
| AiPlayerbot.MaxWaitForMove | 5000 | Max wait time when moving (ms) |
| AiPlayerbot.DisableMoveSplinePath | 0 | Disable MoveSplinePath for bots (0 = enabled, 1 = BG/Arena only, 2 = everywhere) |
| AiPlayerbot.MaxMovementSearchTime | 3 | Max time to search for movement path (higher improves slope navigation) |
| AiPlayerbot.ExpireActionTime | 5000 | Action expiration time (ms) |
| AiPlayerbot.DispelAuraDuration | 700 | Max aura duration to consider for dispel (ms) |
| AiPlayerbot.ReactDelay | 100 | Delay between two bot actions (ms) |
| AiPlayerbot.DynamicReactDelay | 1 | Dynamically adjust react delay by bot status to reduce server lag |
| AiPlayerbot.PassiveDelay | 10000 | Inactivity delay (ms) |
| AiPlayerbot.RepeatDelay | 2000 | Minimum delay between repeating actions like chat or emotes (ms) |
| AiPlayerbot.ErrorDelay | 100 | Delay after an error before retrying (ms) |
| AiPlayerbot.RpgDelay | 10000 | Delay between RPG actions (ms) |
| AiPlayerbot.SitDelay | 20000 | Delay before sitting (ms) |
| AiPlayerbot.ReturnDelay | 2000 | Delay before returning (ms, minimum 2000 — lower values crash) |
| AiPlayerbot.LootDelay | 1000 | Delay before looting (ms) |

---

## PLAYERBOTS SETTINGS — DISTANCES

All distances are in yards.

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.FarDistance | 20.0 | Distance considered "far" for bot positioning |
| AiPlayerbot.SightDistance | 100.0 | Maximum sight range for bots |
| AiPlayerbot.SpellDistance | 28.5 | Maximum distance for casting spells |
| AiPlayerbot.ShootDistance | 5.0 | Minimum distance to start shooting |
| AiPlayerbot.HealDistance | 38.5 | Maximum distance to heal a target |
| AiPlayerbot.LootDistance | 15.0 | Maximum distance to loot |
| AiPlayerbot.FleeDistance | 5.0 | Distance to flee from danger |
| AiPlayerbot.AggroDistance | 22 | Distance at which bots engage enemies |
| AiPlayerbot.TooCloseDistance | 5.0 | Distance considered too close to a target |
| AiPlayerbot.MeleeDistance | 0.75 | Distance for melee attacks |
| AiPlayerbot.FollowDistance | 1.5 | Distance bots maintain while following the master |
| AiPlayerbot.WhisperDistance | 6000.0 | Maximum distance for whisper range |
| AiPlayerbot.ContactDistance | 0.45 | Distance for direct contact interactions |
| AiPlayerbot.AoeRadius | 10 | Radius for AoE spell targeting |
| AiPlayerbot.RpgDistance | 200 | Distance for RPG movement actions |
| AiPlayerbot.GrindDistance | 75.0 | Distance bots search for grind targets |
| AiPlayerbot.ReactDistance | 150.0 | Distance at which bots react to nearby events |

---

## PLAYERBOTS SETTINGS — THRESHOLDS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.CriticalHealth | 25 | Health percentage considered critical |
| AiPlayerbot.LowHealth | 45 | Health percentage considered low |
| AiPlayerbot.MediumHealth | 65 | Health percentage considered medium |
| AiPlayerbot.AlmostFullHealth | 85 | Health percentage considered almost full |
| AiPlayerbot.LowMana | 15 | Mana percentage considered low |
| AiPlayerbot.MediumMana | 40 | Mana percentage considered medium |
| AiPlayerbot.HighMana | 65 | Mana percentage considered high |

---

## PLAYERBOTS SETTINGS — QUESTS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.AutoPickReward | yes | How bots pick quest rewards: `yes` = most useful, `no` = list all, `ask` = useful + list if multiple |
| AiPlayerbot.SyncQuestWithPlayer | 1 | Bots complete quests the moment the player hands them in |
| AiPlayerbot.SyncQuestForPlayer | 0 | Bots auto-complete quests for the player when handing in their own quests |
| AiPlayerbot.DropObsoleteQuests | 1 | Bots automatically drop obsolete quests |

---

## PLAYERBOTS SETTINGS — COMBAT

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.ApplyInstanceStrategies | 1 | Auto-apply dungeon/raid strategies when entering an instance |
| AiPlayerbot.AutoAvoidAoe | 1 | Enable automatic AoE avoidance strategy |
| AiPlayerbot.MaxAoeAvoidRadius | 15.0 | Only avoid AoE spells with a radius smaller than this value (yards) |
| AiPlayerbot.AoeAvoidSpellWhitelist | 50759,57491,13810,29946 | Comma-separated spell IDs that bots should never attempt to avoid |
| AiPlayerbot.AutoSaveMana | 1 | Enable healer bot save-mana strategy |
| AiPlayerbot.SaveManaThreshold | 60 | Mana percentage at which healer bots switch to save-mana mode |
| AiPlayerbot.FleeingEnabled | 1 | Allow bots to flee from enemies |

---

## PLAYERBOTS SETTINGS — GREATER BUFFS STRATEGIES

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.MinBotsForGreaterBuff | 3 | Minimum group size before Paladins/Mages/Druids use Greater buff variants |
| AiPlayerbot.RPWarningCooldown | 30 | Seconds between reagent-missing RP warnings, per bot and per buff |

---

## PLAYERBOTS SETTINGS — CHEATS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.MaintenanceCommand | 1 | Enable the maintenance command (learn spells, assign talents, repair, enchant, etc.) |
| AiPlayerbot.AltMaintenanceAmmo | 1 | Allow maintenance to provide free ammo to alt bots |
| AiPlayerbot.AltMaintenanceFood | 1 | Allow maintenance to provide free food to alt bots |
| AiPlayerbot.AltMaintenanceReagents | 1 | Allow maintenance to provide free reagents to alt bots |
| AiPlayerbot.AltMaintenanceConsumables | 1 | Allow maintenance to provide free consumables to alt bots |
| AiPlayerbot.AltMaintenancePotions | 1 | Allow maintenance to provide free potions to alt bots |
| AiPlayerbot.AltMaintenanceBags | 1 | Allow maintenance to provide free bags to alt bots |
| AiPlayerbot.AltMaintenanceMounts | 1 | Allow maintenance to provide free mounts to alt bots |
| AiPlayerbot.AltMaintenanceSkills | 1 | Allow maintenance to grant free skill levels to alt bots |
| AiPlayerbot.AltMaintenanceClassSpells | 1 | Allow maintenance to grant class spells to alt bots |
| AiPlayerbot.AltMaintenanceAvailableSpells | 1 | Allow maintenance to grant available trainable spells to alt bots |
| AiPlayerbot.AltMaintenanceSpecialSpells | 1 | Allow maintenance to grant special spells (RandomBotSpellIds + DK Death Gate) to alt bots |
| AiPlayerbot.AltMaintenanceTalentTree | 1 | Allow maintenance to assign talent points for alt bots |
| AiPlayerbot.AltMaintenanceGlyphs | 1 | Allow maintenance to apply glyphs to alt bots |
| AiPlayerbot.AltMaintenanceGemsEnchants | 1 | Allow maintenance to socket gems and apply enchants to alt bots |
| AiPlayerbot.AltMaintenancePet | 1 | Allow maintenance to provide free pets to alt bots |
| AiPlayerbot.AltMaintenancePetTalents | 1 | Allow maintenance to assign pet talents for alt bots |
| AiPlayerbot.AltMaintenanceReputation | 1 | Allow maintenance to set reputation for alt bots |
| AiPlayerbot.AltMaintenanceAttunementQuests | 1 | Allow maintenance to complete attunement quests for alt bots |
| AiPlayerbot.AltMaintenanceKeyring | 1 | Allow maintenance to provide keyring items for alt bots |
| AiPlayerbot.AutoGearCommand | 1 | Enable the autogear command to automatically upgrade bot gear |
| AiPlayerbot.AutoGearCommandAltBots | 1 | Enable autogear command for alt bots |
| AiPlayerbot.AutoGearQualityLimit | 3 | Maximum item quality for autogear (1=normal, 2=uncommon, 3=rare, 4=epic, 5=legendary) |
| AiPlayerbot.AutoGearScoreLimit | 0 | Maximum item level for autogear (0 = no limit) |
| AiPlayerbot.BotCheats | "food,taxi,raid" | Comma-separated list of enabled cheats: food, gold, health, mana, power, taxi, raid |
| AiPlayerbot.AttunementQuests | (long list) | Comma-separated quest IDs auto-completed for all bots to bypass attunement requirements |

---

## PLAYERBOTS SETTINGS — FLIGHTPATH

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.BotTaxiDelayMinMs | 350 | Minimum random delay before the first follower bot clicks the flight master (ms) |
| AiPlayerbot.BotTaxiDelayMaxMs | 5000 | Upper bound for the overall taxi delay window, spreads large raids (ms) |
| AiPlayerbot.BotTaxiGapMs | 200 | Fixed gap added per group slot so bots never take off together (ms) |
| AiPlayerbot.BotTaxiGapJitterMs | 100 | Extra random jitter per gap slot to prevent robotic launches (ms) |

---

## PLAYERBOTS SETTINGS — PROFESSIONS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.ClassMatchingProfessionChance | 30 | Percentage of randombots per class bucket that receive a class-matching profession combination |
| AiPlayerbot.EnableFishingWithMaster | 1 | Automatically add the master fishing strategy to fishing bots when they fish with a master |
| AiPlayerbot.FishingDistanceFromMaster | 10.0 | Distance (yards) a bot with a master searches for fishable water |
| AiPlayerbot.FishingDistance | 40.0 | Distance (yards) a masterless bot searches for fishable water (currently unused) |
| AiPlayerbot.EndFishingWithMaster | 30.0 | Distance from water (yards) beyond which a bot removes its master fishing strategy |

---

## RANDOMBOT-SPECIFIC SETTINGS — GENERAL

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.RandomBotRandomPassword | 0 | Use a randomly generated password for randombot accounts |
| AiPlayerbot.RandomBotAccountPrefix | "rndbot" | Prefix for randombot account names |
| AiPlayerbot.RandomBotMinLevel | 1 | Minimum level for randombots |
| AiPlayerbot.RandomBotMaxLevel | 80 | Maximum level for randombots |
| AiPlayerbot.SyncLevelWithPlayers | 0 | Sync max randombot level with the max level of currently online players |
| AiPlayerbot.PreQuests | 0 | Mark many quests at or below bot level as complete on bot creation (slows creation) |
| AiPlayerbot.RandomBotJoinLfg | 1 | Enable LFG queue participation for randombots |
| AiPlayerbot.EnablePeriodicOnlineOffline | 0 | Enable randombots periodically going online/offline to simulate player patterns |
| AiPlayerbot.PeriodicOnlineOfflineRatio | 2.0 | Ratio of total bots (including offline) to MaxRandomBots; only applies when EnablePeriodicOnlineOffline is on |
| AiPlayerbot.RandomBotAllianceRatio | 50 | Percentage of randombots that are Alliance |
| AiPlayerbot.RandomBotHordeRatio | 50 | Percentage of randombots that are Horde |
| AiPlayerbot.DisableDeathKnightLogin | 0 | Prevent Death Knight randombots from logging in |
| AiPlayerbot.LimitTalentsExpansion | 0 | Simulate expansion-limited talent trees based on bot level |
| AiPlayerbot.EnableRandomBotTrading | 1 | Configure randombot trading (0=off, 1=both, 2=buy only, 3=sell only) |
| AiPlayerbot.TradeActionExcludedPrefixes | (addon list) | Message prefixes excluded from trade-window trigger analysis |

---

## RANDOMBOT-SPECIFIC SETTINGS — LEVELS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.DisableRandomLevels | 0 | Disable random level generation; all bots start at RandombotStartingLevel |
| AiPlayerbot.RandombotStartingLevel | 1 | Starting level for randombots when DisableRandomLevels is enabled |
| AiPlayerbot.RandomBotMinLevelChance | 0.1 | Probability (0–1) a randombot starts at the minimum level on first randomize |
| AiPlayerbot.RandomBotMaxLevelChance | 0.1 | Probability (0–1) a randombot starts at the maximum level on first randomize |
| AiPlayerbot.RandomBotFixedLevel | 0 | Freeze bot levels so they cannot level up |
| AiPlayerbot.DowngradeMaxLevelBot | 0 | Reset max-level bots back to RandomBotMinLevel |
| AiPlayerbot.RandomBotXPRate | 1.0 | XP rate multiplier for randombots (multiplied by server rate) |

---

## RANDOMBOT-SPECIFIC SETTINGS — GEAR

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.RandomGearQualityLimit | 3 | Maximum gear quality for randomly generated randombot equipment (1–5) |
| AiPlayerbot.RandomGearScoreLimit | 0 | Maximum item level for randombot gear (0 = no limit) |
| AiPlayerbot.PreferClassArmorType | 0 | Apply 3x score multiplier to class-appropriate armor type during gear selection |
| AiPlayerbot.PreferredSpecWeapons | 0 | Prefer spec-appropriate weapon types during autogear (e.g., slow 2H for Arms Warriors) |
| AiPlayerbot.IncrementalGearInit | 1 | Randombots upgrade gear incrementally through loot and quests (0 = loot/quest only) |
| AiPlayerbot.MinEnchantingBotLevel | 60 | Minimum bot level to apply enchants and socket gems via maintenance |
| AiPlayerbot.LimitEnchantExpansion | 1 | Bots do not use TBC enchants before level 61 or WotLK enchants/gems before level 71 |
| AiPlayerbot.LimitGearExpansion | 1 | Bots do not equip TBC gear before level 61 or WotLK gear before level 71 |
| AiPlayerbot.RandomGearLoweringChance | 0 | Chance (0–1) a randombot receives suboptimal gear on initialization |
| AiPlayerbot.UnobtainableItems | 12468,46978 | Comma-separated item IDs excluded from bot gear generation |
| AiPlayerbot.GearScoreCheck | 0 | Randombots deny group invitations from players with too-low gear score |
| AiPlayerbot.EquipmentPersistence | 0 | Stop random gear re-initialization after EquipmentPersistenceLevel |
| AiPlayerbot.EquipmentPersistenceLevel | 80 | Level at which equipment persistence kicks in |
| AiPlayerbot.AutoUpgradeEquip | 1 | Randombots automatically upgrade equipment on level-up |
| AiPlayerbot.HunterWolfPet | 0 | Force wolf pets for hunters (0=disabled, 1=max-level only, 2=always) |
| AiPlayerbot.DefaultPetStance | 1 | Default pet stance on summon (0=Passive, 1=Defensive, 2=Aggressive) |
| AiPlayerbot.PetChatCommandDebug | 0 | Enable debug messages for pet commands |
| AiPlayerbot.ExcludedHunterPetFamilies | "" | Comma-separated creature family IDs prohibited as hunter bot pets |

---

## RANDOMBOT-SPECIFIC SETTINGS — ACTIVITY

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.BotActiveAlone | 10 | Approximate percentage of bots that are active when no real players are nearby |
| AiPlayerbot.BotActiveAloneDurationSeconds | 30 | How often (seconds) the active bot roster rotates |
| AiPlayerbot.BotActiveAloneForceWhenInRadius | 150 | Force-activate bots within this many yards of a real player (0 = disabled) |
| AiPlayerbot.BotActiveAloneForceWhenInZone | 1 | Force-activate bots in the same zone as a real player |
| AiPlayerbot.BotActiveAloneForceWhenInMap | 0 | Force-activate bots on the same continent as a real player |
| AiPlayerbot.BotActiveAloneForceWhenIsFriend | 0 | Force-activate bots on a real player's friends list |
| AiPlayerbot.BotActiveAloneForceWhenInGuild | 1 | Force-activate bots in a guild that has at least one real player |
| AiPlayerbot.botActiveAloneSmartScale | 1 | Automatically reduce active bots when server tick time is high |
| AiPlayerbot.botActiveAloneSmartScaleDiffLimitfloor | 50 | Server update time (ms) below which no SmartScale reduction occurs |
| AiPlayerbot.botActiveAloneSmartScaleDiffLimitCeiling | 200 | Server update time (ms) at which all non-forced bots are paused |
| AiPlayerbot.botActiveAloneSmartScaleWhenMinLevel | 1 | Minimum bot level affected by SmartScale |
| AiPlayerbot.botActiveAloneSmartScaleWhenMaxLevel | 80 | Maximum bot level affected by SmartScale |

---

## RANDOMBOT-SPECIFIC SETTINGS — QUESTS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.RandomBotQuestIds | (long list) | Quest IDs automatically completed and rewarded for all randombots |
| AiPlayerbot.RandomBotGroupNearby | 0 | Randombots will group with nearby randombots to complete shared quests |
| AiPlayerbot.AutoDoQuests | 1 | Randombots pick up quests on their own and attempt to complete them |
| AiPlayerbot.RandomBotQuestItems | (item ID list) | Item IDs that bots will not destroy from their inventories |

---

## RANDOMBOT-SPECIFIC SETTINGS — SPELLS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.AutoLearnQuestSpells | 0 | Randombots automatically learn class quest reward spells on level-up |
| AiPlayerbot.AutoLearnTrainerSpells | 1 | Randombots automatically learn trainable spells on level-up |
| AiPlayerbot.AutoPickTalents | 1 | Randombots automatically spend talent points on level-up |
| AiPlayerbot.RandomBotSpellIds | "54197" | Comma-separated spell IDs every randombot learns automatically (default: Cold Weather Flying) |
| AiPlayerbot.OpenGoSpell | 6477 | Spell ID used to open lootable chests |

---

## RANDOMBOT-SPECIFIC SETTINGS — STRATEGIES

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.RandomBotCombatStrategies | "" | Additional combat strategies applied to all randombots (e.g., `"+threat,-potions"`) |
| AiPlayerbot.RandomBotNonCombatStrategies | "" | Additional non-combat strategies applied to all randombots |
| AiPlayerbot.CombatStrategies | "" | Additional combat strategies applied to all altbots |
| AiPlayerbot.NonCombatStrategies | "" | Additional non-combat strategies applied to all altbots |
| AiPlayerbot.HealerDPSMapRestriction | 1 | Remove "healer dps" strategy on specified maps |
| AiPlayerbot.RestrictedHealerDPSMaps | (dungeon/raid map ID list) | Map IDs where healer DPS strategy is removed when HealerDPSMapRestriction is enabled |

---

## RANDOMBOT-SPECIFIC SETTINGS — RPG STRATEGY

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.EnableNewRpgStrategy | 1 | Enable the RPG strategy; overrides AutoDoQuests, RandomBotTeleLowerLevel, RandomBotTeleHigherLevel |
| AiPlayerbot.RpgStatusProbWeight.WanderRandom | 15 | Weight for "wander randomly to find and kill mobs" RPG status |
| AiPlayerbot.RpgStatusProbWeight.WanderNpc | 20 | Weight for "randomly interact with nearby NPCs" RPG status |
| AiPlayerbot.RpgStatusProbWeight.GoGrind | 15 | Weight for "go to level-appropriate area to grind" RPG status |
| AiPlayerbot.RpgStatusProbWeight.GoCamp | 10 | Weight for "return to nearby camp near innkeeper/flightmaster" RPG status |
| AiPlayerbot.RpgStatusProbWeight.DoQuest | 60 | Weight for "select and head to quest objective" RPG status |
| AiPlayerbot.RpgStatusProbWeight.TravelFlight | 15 | Weight for "fly to level-appropriate area" RPG status |
| AiPlayerbot.RpgStatusProbWeight.Rest | 5 | Weight for "do nothing for a while" RPG status |
| AiPlayerbot.RpgStatusProbWeight.OutdoorPvp | 10 | Weight for "participate in outdoor PvP capture points" RPG status |

### ZoneBracket entries

`AiPlayerbot.ZoneBracket.<zoneId> = minLevel,maxLevel` sets the level range for randombots using the RPG strategy in each zone. Bots teleport out of a zone if their level falls outside the bracket. Format: `"min,max"`.

Examples:
- `AiPlayerbot.ZoneBracket.12 = 5,12` — Elwynn Forest, levels 5–12
- `AiPlayerbot.ZoneBracket.3483 = 58,66` — Hellfire Peninsula, levels 58–66
- `AiPlayerbot.ZoneBracket.3537 = 68,75` — Borean Tundra, levels 68–75
- `AiPlayerbot.ZoneBracket.66 = 74,80` — Zul'Drak, levels 74–80

See `docs/configs/playerbots.conf.dist` for the full list covering all Classic, TBC, and WotLK zones (~40 entries total).

---

## RANDOMBOT-SPECIFIC SETTINGS — TELEPORTS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.RandomBotMaps | 0,1,530,571 | Map IDs bots may teleport to (EK, Kalimdor, Outland, Northrend) |
| AiPlayerbot.ProbTeleToBankers | 0.25 | Probability a bot teleports to a city banker |
| AiPlayerbot.EnableWeightTeleToCityBankers | 1 | Use weighted city selection for banker teleports |
| AiPlayerbot.TeleToStormwindWeight | 2 | Weight for teleporting to Stormwind |
| AiPlayerbot.TeleToIronforgeWeight | 1 | Weight for teleporting to Ironforge |
| AiPlayerbot.TeleToDarnassusWeight | 1 | Weight for teleporting to Darnassus |
| AiPlayerbot.TeleToExodarWeight | 1 | Weight for teleporting to the Exodar |
| AiPlayerbot.TeleToOrgrimmarWeight | 2 | Weight for teleporting to Orgrimmar |
| AiPlayerbot.TeleToUndercityWeight | 1 | Weight for teleporting to Undercity |
| AiPlayerbot.TeleToThunderBluffWeight | 1 | Weight for teleporting to Thunder Bluff |
| AiPlayerbot.TeleToSilvermoonCityWeight | 1 | Weight for teleporting to Silvermoon City |
| AiPlayerbot.TeleToShattrathCityWeight | 1 | Weight for teleporting to Shattrath City |
| AiPlayerbot.TeleToDalaranWeight | 1 | Weight for teleporting to Dalaran |
| AiPlayerbot.RandomBotTeleportDistance | 100 | Distance (yards) randombots teleport after death |
| AiPlayerbot.RandomBotTeleLowerLevel | 1 | Levels below zone minimum a bot can be before teleporting out (no effect when NewRpgStrategy enabled) |
| AiPlayerbot.RandomBotTeleHigherLevel | 3 | Levels above zone maximum a bot can be before teleporting out (no effect when NewRpgStrategy enabled) |
| AiPlayerbot.AutoTeleportForLevel | 1 | Bots automatically teleport to a new leveling area on level-up |

---

## RANDOMBOT-SPECIFIC SETTINGS — BATTLEGROUND & ARENA & PVP

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.RandomBotJoinBG | 1 | Enable battleground and arena participation for randombots |
| AiPlayerbot.RandomBotAutoJoinBG | 0 | Bots auto-start BGs and arenas on their own |
| AiPlayerbot.RandomBotAutoJoinICBrackets | 1 | Isle of Conquest bracket(s) for auto-join |
| AiPlayerbot.RandomBotAutoJoinEYBrackets | 2 | Eye of the Storm bracket(s) for auto-join |
| AiPlayerbot.RandomBotAutoJoinAVBrackets | 3 | Alterac Valley bracket(s) for auto-join |
| AiPlayerbot.RandomBotAutoJoinABBrackets | 6 | Arathi Basin bracket(s) for auto-join |
| AiPlayerbot.RandomBotAutoJoinWSBrackets | 7 | Warsong Gulch bracket(s) for auto-join |
| AiPlayerbot.RandomBotAutoJoinBGICCount | 0 | Number of IoC instances per bracket |
| AiPlayerbot.RandomBotAutoJoinBGEYCount | 1 | Number of EotS instances per bracket |
| AiPlayerbot.RandomBotAutoJoinBGAVCount | 0 | Number of AV instances per bracket |
| AiPlayerbot.RandomBotAutoJoinBGABCount | 1 | Number of AB instances per bracket |
| AiPlayerbot.RandomBotAutoJoinBGWSCount | 1 | Number of WSG instances per bracket |
| AiPlayerbot.RandomBotAutoJoinArenaBracket | 14 | Rated arena bracket for auto-join (14 = level 80) |
| AiPlayerbot.RandomBotAutoJoinBGRatedArena2v2Count | 0 | Number of rated 2v2 arenas to fill per bracket |
| AiPlayerbot.RandomBotAutoJoinBGRatedArena3v3Count | 0 | Number of rated 3v3 arenas to fill per bracket |
| AiPlayerbot.RandomBotAutoJoinBGRatedArena5v5Count | 0 | Number of rated 5v5 arenas to fill per bracket |
| AiPlayerbot.RandomBotArenaTeam2v2Count | 10 | Number of 2v2 bot arena teams created on server start |
| AiPlayerbot.RandomBotArenaTeam3v3Count | 10 | Number of 3v3 bot arena teams created on server start |
| AiPlayerbot.RandomBotArenaTeam5v5Count | 5 | Number of 5v5 bot arena teams created on server start |
| AiPlayerbot.RandomBotArenaTeamMaxRating | 2000 | Maximum randomized rating for new bot arena teams |
| AiPlayerbot.RandomBotArenaTeamMinRating | 1000 | Minimum randomized rating for new bot arena teams |
| AiPlayerbot.DeleteRandomBotArenaTeams | 0 | Set to 1 to delete all randombot arena teams on next server start |
| AiPlayerbot.PvpProhibitedZoneIds | (long list) | Zone IDs where bots will not engage in PvP |
| AiPlayerbot.PvpProhibitedAreaIds | (long list) | Area IDs where bots will not engage in PvP |
| AiPlayerbot.FastReactInBG | 1 | Improve bot reaction speed in battlegrounds and arenas (may cause lag) |

---

## RANDOMBOT-SPECIFIC SETTINGS — RANDOM BOT TIMING AND BEHAVIOR

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.RandomBotUpdateInterval | 20 | Seconds between random bot manager main update loop runs |
| AiPlayerbot.RandomBotCountChangeMinInterval | 1800 | Minimum seconds before manager re-evaluates total bot count |
| AiPlayerbot.RandomBotCountChangeMaxInterval | 7200 | Maximum seconds before manager re-evaluates total bot count |
| AiPlayerbot.MinRandomBotInWorldTime | 600 | Minimum seconds a randombot stays online before logging out |
| AiPlayerbot.MaxRandomBotInWorldTime | 28800 | Maximum seconds a randombot stays online before logging out |
| AiPlayerbot.MinRandomBotRandomizeTime | 7200 | Minimum seconds before a bot is eligible for re-randomization |
| AiPlayerbot.MaxRandomBotRandomizeTime | 1209600 | Maximum seconds before a bot is eligible for re-randomization |
| AiPlayerbot.RandomBotsPerInterval | 60 | Number of bots processed (login/logout/update) per manager update cycle |
| AiPlayerbot.MinRandomBotReviveTime | 60 | Minimum seconds after death before a bot revives |
| AiPlayerbot.MaxRandomBotReviveTime | 300 | Maximum seconds after death before a bot revives |
| AiPlayerbot.MinRandomBotTeleportInterval | 3600 | Minimum seconds between bot teleports to new areas |
| AiPlayerbot.MaxRandomBotTeleportInterval | 18000 | Maximum seconds between bot teleports to new areas |
| AiPlayerbot.PermanentlyInWorldTime | 31104000 | Seconds a "permanent" bot stays in the world (~1 year) |

---

## PREMADE SPECS

Premade spec entries define the talent builds, glyphs, and gear progression used by bots. They use three key families:

- `AiPlayerbot.PremadeSpecName.<classId>.<specIndex>` — human-readable name for the spec (e.g., `"arms pve"`)
- `AiPlayerbot.PremadeSpecGlyph.<classId>.<specIndex>` — comma-separated glyph item IDs in order: major1, minor1, major2, minor2, minor3, major3
- `AiPlayerbot.PremadeSpecLink.<classId>.<specIndex>.<level>` — Wowhead-style talent link string the bot works toward at the given level

Class IDs: 1=Warrior, 2=Paladin, 3=Hunter, 4=Rogue, 5=Priest, 6=Death Knight, 7=Shaman, 8=Mage, 9=Warlock, 11=Druid. Spec indexes start at 0. Level breakpoints are typically 40, 60, 65, 70, 80 (not all are defined for every spec).

Hunter bots also support `AiPlayerbot.PremadeHunterPetLink.<petType>.<level>` for pet talent builds (0=Ferocity, 1=Tenacity, 2=Cunning).

To customize bot talent builds, edit the appropriate PremadeSpecLink entry for the desired class, spec, and level. Build the talent string at https://www.wowhead.com/wotlk/talent-calc. See `docs/configs/playerbots.conf.dist` for all entries (Warrior specs 0–5, Paladin 0–5, Hunter 0–5, Rogue 0–5, Priest 0–5, DK 0–6, Shaman 0–5, Mage 0–6, Warlock 0–5, Druid 0–6).

---

## WORLD BUFFS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.WorldBuffMatrix | (large matrix) | Defines auto-refreshing world buffs applied to bots by faction, class, spec, and level range. Format: `Entry:FactionID,ClassID,SpecID,MinLevel,MaxLevel:SpellID1,SpellID2,...;`. FactionID 0 = both factions. Requires `nc +worldbuff` command sent to the bot to activate. |

The default matrix applies level-appropriate Vanilla (60–69), TBC (70–79), and WotLK (80) world buff simulations (flasks, food, runes) for every implemented PvE spec. Custom entries can be added or existing ones modified.

---

## RANDOMBOT DEFAULT TALENT SPECS

These control which PremadeSpec index each randombot class picks and with what probability.

`AiPlayerbot.RandomClassSpecProb.<classId>.<specIndex>` — probability weight for selecting this spec (weights need not sum to 100; set to 0 to disable).
`AiPlayerbot.RandomClassSpecIndex.<classId>.<specIndex>` — which PremadeSpec index to use when this spec is selected.

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
| AiPlayerbot.CommandServerPort | 8888 | TCP port for the command server (0 = disabled) |

---

## PLAYERBOTS SYSTEM SETTINGS — DEBUG

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.SpellDump | 0 | Dump spell information for debugging |
| AiPlayerbot.LogInGroupOnly | 1 | Only log bot actions when in a group |
| AiPlayerbot.LogValuesPerTick | 0 | Log bot values every tick |
| AiPlayerbot.RandomChangeMultiplier | 1 | Multiplier for random changes |
| AiPlayerbot.TellWhenAvoidAoe | 0 | Bot tells which spell it is avoiding (experimental) |
| AiPlayerbot.PerfMonEnabled | 0 | Enable the performance monitor |

---

## PLAYERBOTS SYSTEM SETTINGS — CHAT SETTINGS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.CommandPrefix | "" | Prefix required before bot chat commands |
| AiPlayerbot.CommandSeparator | "\\\\" | Separator character between chained bot commands |
| AiPlayerbot.EnableAutoTradeOnItemMention | 1 | Automatically show inventory and open trade when item keywords are mentioned in chat |
| AiPlayerbot.RandomBotTalk | 1 | Enable bots talking in say/yell/general/LFG channels |
| AiPlayerbot.RandomBotEmote | 0 | Enable bots performing emotes |
| AiPlayerbot.RandomBotSuggestDungeons | 1 | Randombots suggest dungeons in chat |
| AiPlayerbot.EnableGreet | 0 | Bots greet players when invited to a group |
| AiPlayerbot.ToxicLinksRepliesChance | 30 | Chance (0–100) bots reply to toxic links with their own toxic links |
| AiPlayerbot.ThunderfuryRepliesChance | 40 | Chance (0–100) bots reply to Thunderfury mentions |
| AIPlayerbot.GuildFeedback | 1 | Bots chat in guild about certain in-game events |
| AiPlayerbot.GuildRepliesRate | 100 | Chance (0–100) bots reply in guild chat about events |
| AiPlayerbot.RandomBotSayWithoutMaster | 0 | Masterless bots say their idle lines |

---

## PLAYERBOTS SYSTEM SETTINGS — BROADCAST RATES

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.EnableBroadcasts | 1 | Enable or disable all broadcasts globally |
| AiPlayerbot.BroadcastToGuildGlobalChance | 30000 | Global chance for guild channel broadcasts (0–30000) |
| AiPlayerbot.BroadcastToWorldGlobalChance | 30000 | Global chance for world channel broadcasts |
| AiPlayerbot.BroadcastToGeneralGlobalChance | 30000 | Global chance for general channel broadcasts |
| AiPlayerbot.BroadcastToTradeGlobalChance | 30000 | Global chance for trade channel broadcasts |
| AiPlayerbot.BroadcastToLFGGlobalChance | 30000 | Global chance for LFG channel broadcasts |
| AiPlayerbot.BroadcastToLocalDefenseGlobalChance | 30000 | Global chance for local defense channel broadcasts |
| AiPlayerbot.BroadcastToWorldDefenseGlobalChance | 30000 | Global chance for world defense channel broadcasts |
| AiPlayerbot.BroadcastToGuildRecruitmentGlobalChance | 30000 | Global chance for guild recruitment channel broadcasts |
| AiPlayerbot.BroadcastChanceLootingItemPoor | 30 | Chance bot broadcasts looting a poor-quality item |
| AiPlayerbot.BroadcastChanceLootingItemNormal | 150 | Chance bot broadcasts looting a normal-quality item |
| AiPlayerbot.BroadcastChanceLootingItemUncommon | 10000 | Chance bot broadcasts looting an uncommon item |
| AiPlayerbot.BroadcastChanceLootingItemRare | 20000 | Chance bot broadcasts looting a rare item |
| AiPlayerbot.BroadcastChanceLootingItemEpic | 30000 | Chance bot broadcasts looting an epic item |
| AiPlayerbot.BroadcastChanceLootingItemLegendary | 30000 | Chance bot broadcasts looting a legendary item |
| AiPlayerbot.BroadcastChanceLootingItemArtifact | 30000 | Chance bot broadcasts looting an artifact item |
| AiPlayerbot.BroadcastChanceQuestAccepted | 6000 | Chance bot broadcasts accepting a quest |
| AiPlayerbot.BroadcastChanceQuestUpdateObjectiveCompleted | 300 | Chance bot broadcasts completing a quest objective |
| AiPlayerbot.BroadcastChanceQuestUpdateObjectiveProgress | 300 | Chance bot broadcasts quest objective progress |
| AiPlayerbot.BroadcastChanceQuestUpdateFailedTimer | 300 | Chance bot broadcasts a failed quest timer |
| AiPlayerbot.BroadcastChanceQuestUpdateComplete | 1000 | Chance bot broadcasts completing a quest |
| AiPlayerbot.BroadcastChanceQuestTurnedIn | 10000 | Chance bot broadcasts turning in a quest |
| AiPlayerbot.BroadcastChanceKillNormal | 30 | Chance bot broadcasts killing a normal mob |
| AiPlayerbot.BroadcastChanceKillElite | 300 | Chance bot broadcasts killing an elite mob |
| AiPlayerbot.BroadcastChanceKillRareelite | 3000 | Chance bot broadcasts killing a rare elite |
| AiPlayerbot.BroadcastChanceKillWorldboss | 20000 | Chance bot broadcasts killing a world boss |
| AiPlayerbot.BroadcastChanceKillRare | 10000 | Chance bot broadcasts killing a rare mob |
| AiPlayerbot.BroadcastChanceKillUnknown | 100 | Chance bot broadcasts killing an unknown mob type |
| AiPlayerbot.BroadcastChanceKillPet | 10 | Chance bot broadcasts killing a pet |
| AiPlayerbot.BroadcastChanceKillPlayer | 30 | Chance bot broadcasts killing a player |
| AiPlayerbot.BroadcastChanceLevelupGeneric | 20000 | Chance bot broadcasts a generic level-up |
| AiPlayerbot.BroadcastChanceLevelupTenX | 30000 | Chance bot broadcasts a level that is a multiple of 10 |
| AiPlayerbot.BroadcastChanceLevelupMaxLevel | 30000 | Chance bot broadcasts reaching max level |
| AiPlayerbot.BroadcastChanceSuggestInstance | 5000 | Chance bot broadcasts a dungeon/raid suggestion |
| AiPlayerbot.BroadcastChanceSuggestQuest | 10000 | Chance bot broadcasts a quest suggestion |
| AiPlayerbot.BroadcastChanceSuggestGrindMaterials | 5000 | Chance bot broadcasts a grind materials suggestion |
| AiPlayerbot.BroadcastChanceSuggestGrindReputation | 5000 | Chance bot broadcasts a reputation grind suggestion |
| AiPlayerbot.BroadcastChanceSuggestSell | 300 | Chance bot broadcasts a selling suggestion |
| AiPlayerbot.BroadcastChanceSuggestSomething | 30000 | Chance bot broadcasts a generic suggestion |
| AiPlayerbot.BroadcastChanceSuggestSomethingToxic | 0 | Chance bot says rude things (disabled by default) |
| AiPlayerbot.BroadcastChanceSuggestToxicLinks | 0 | Chance bot says `"<word> [item link]"` toxic lines (disabled by default) |
| AiPlayerbot.ToxicLinksPrefix | gnomes | Word used as prefix in toxic item link broadcasts |
| AiPlayerbot.BroadcastChanceSuggestThunderfury | 1 | Chance bot suggests Thunderfury |
| AiPlayerbot.BroadcastChanceGuildManagement | 30000 | Chance bot broadcasts guild management messages (ignores global chance) |

---

## PLAYERBOTS SYSTEM SETTINGS — LOGS

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.AllowedLogFiles | "" | Comma-separated list of log filenames bots are allowed to create |
| AiPlayerbot.DisallowedGameObjects | (long GUID list) | Comma-separated game object GUIDs bots are not allowed to interact with |

---

## DEPRECATED SETTINGS (still in use)

| Key | Default | Description |
|-----|---------|-------------|
| AiPlayerbot.RandomBotLoginAtStartup | 1 | Log on all randombots at server startup |
| AiPlayerbot.EnableGuildTasks | 0 | Enable the guild task system |
| AiPlayerbot.SuggestDungeonsInLowerCaseRandomly | 0 | Randomly suggest dungeons in lower case |
| AiPlayerbot.RandomBotRpgChance | 0.20 | Chance bot teleports to random camp for level instead of grinding (legacy RPG strategy) |
| AiPlayerbot.RandombotsWalkingRPG | 0 | Force randombots to walk everywhere |
| AiPlayerbot.RandombotsWalkingRPG.InDoors | 0 | Force randombots to walk inside buildings only |
| AiPlayerbot.PremadeAvoidAoe | 62234-4 | Premade AoE spell avoid entries for undetected spells (spellid-radius format) |
| AiPlayerbot.MinRandomBotsPriceChangeInterval | 7200 | Minimum seconds between randombot AH price changes |
| AiPlayerbot.MaxRandomBotsPriceChangeInterval | 172800 | Maximum seconds between randombot AH price changes |
| AiPlayerbot.MinRandomBotChangeStrategyTime | 180 | Minimum seconds before bots change strategy |
| AiPlayerbot.MaxRandomBotChangeStrategyTime | 720 | Maximum seconds before bots change strategy |
| AiPlayerbot.MinGuildTaskChangeTime | 172800 | Minimum seconds between guild task changes |
| AiPlayerbot.MaxGuildTaskChangeTime | 432000 | Maximum seconds between guild task changes |
| AiPlayerbot.MinGuildTaskAdvertisementTime | 300 | Minimum seconds between guild task mail advertisements |
| AiPlayerbot.MaxGuildTaskAdvertisementTime | 28800 | Maximum seconds between guild task mail advertisements |
| AiPlayerbot.MinGuildTaskRewardTime | 300 | Minimum seconds before task reward mail is sent |
| AiPlayerbot.MaxGuildTaskRewardTime | 3600 | Maximum seconds before task reward mail is sent |
| AiPlayerbot.GuildTaskAdvertCleanupTime | 300 | Seconds between guild task advertisement cleanup runs |
| AiPlayerbot.GuildTaskKillTaskDistance | 200 | Maximum distance between victim and bot when creating a guild kill task |
| AiPlayerbot.TargetPosRecalcDistance | 0.1 | Distance margin for facade (positioning) calculations |
| AiPlayerbot.SummonAtInnkeepersEnabled | 1 | Allow bots to be summoned near innkeepers |
| AiPlayerbot.EnableICCBuffs | 1 | Apply ICC helper buffs on PP, Sindragosa, and Lich King to make Heroic mode more accessible |
