# worldserver.conf Reference

> Source of truth: `docs/configs/worldserver.conf.dist`
> Set these via AC_* env vars in `docker-compose.override.yml` (see `ref-config-worldserver.md` for how env var names are derived).

---

## DATABASE & CONNECTIONS

| Key | Default | Description |
|-----|---------|-------------|
| RealmID | 1 | ID of this realm; must match `auth.realmlist` in the auth database |
| WorldServerPort | 8085 | TCP port the world server listens on |
| BindIP | "0.0.0.0" | IP/hostname to bind the world server socket to |
| LoginDatabaseInfo | "127.0.0.1;3306;acore;acore;acore_auth" | Auth DB connection string (hostname;port;user;pass;db) |
| WorldDatabaseInfo | "127.0.0.1;3306;acore;acore;acore_world" | World DB connection string |
| CharacterDatabaseInfo | "127.0.0.1;3306;acore;acore;acore_characters" | Character DB connection string |
| LoginDatabase.WorkerThreads | 1 | Async MySQL worker threads for the auth DB |
| WorldDatabase.WorkerThreads | 1 | Async MySQL worker threads for the world DB |
| CharacterDatabase.WorkerThreads | 1 | Async MySQL worker threads for the character DB |
| LoginDatabase.SynchThreads | 1 | Synchronous MySQL connections for the auth DB |
| WorldDatabase.SynchThreads | 1 | Synchronous MySQL connections for the world DB |
| CharacterDatabase.SynchThreads | 1 | Synchronous MySQL connections for the character DB |
| MaxPingTime | 30 | Interval (minutes) between database keep-alive pings |
| Database.Reconnect.Seconds | 15 | Seconds between reconnection attempts on DB loss |
| Database.Reconnect.Attempts | 20 | Total reconnection attempts before giving up |

---

## DIRECTORIES

| Key | Default | Description |
|-----|---------|-------------|
| DataDir | "." | Path to the data directory (maps, vmaps, mmaps, dbc) |
| LogsDir | "" | Path where log files are written (empty = current dir) |
| TempDir | "" | Path for temporary files |
| CMakeCommand | "" | Path to cmake binary; empty = built-in CMAKE_COMMAND |
| BuildDirectory | "" | Path to the build directory; empty = built-in CMAKE_BINARY_DIR |
| SourceDirectory | "" | Path to AzerothCore source; empty = built-in CMAKE_SOURCE_DIR |
| MySQLExecutable | "" | Path to the mysql CLI binary; empty = cmake-detected default |
| PidFile | "" | Path for world daemon PID file; empty = disabled |

---

## CONSOLE

| Key | Default | Description |
|-----|---------|-------------|
| Console.Enable | 1 | Enable the interactive console (1=yes, 0=no) |
| BeepAtStart | 1 | Beep on Unix/Linux when startup completes |
| FlashAtStart | 1 | Flash taskbar on Windows when startup completes |

---

## AUTOUPDATER

| Key | Default | Description |
|-----|---------|-------------|
| Updates.EnableDatabases | 7 | Bitmask of DBs to auto-update: 1=auth, 2=chars, 4=world (7=all) |
| Updates.AutoSetup | 1 | Auto-populate empty databases on first run |
| Updates.Redundancy | 1 | Hash-check SQL updates to detect changes and reapply them |
| Updates.ArchivedRedundancy | 0 | Also hash-check archived SQL updates (slows startup) |
| Updates.AllowRehash | 1 | Insert file hash when the DB entry is empty (marks file as applied) |
| Updates.CleanDeadRefMaxCount | 3 | Max missing updates to clean up automatically; -1=unlimited, 0=off |
| Updates.ExceptionShutdownDelay | 10000 | Milliseconds to wait before shutdown on a fatal SQL update error |

---

## NETWORK

| Key | Default | Description |
|-----|---------|-------------|
| Network.Threads | 1 | Network handler threads (recommend 1 per 1000 connections) |
| Network.OutKBuff | -1 | Kernel send-buffer size in bytes; -1 = OS default |
| Network.OutUBuff | 4096 | Per-connection user-space output buffer size in bytes |
| Network.TcpNodelay | 1 | 1 = disable Nagle (TCP_NODELAY, less latency); 0 = enable Nagle |
| Network.EnableProxyProtocol | 0 | Enable Proxy Protocol v2 for real-IP tracking behind load balancers |
| Network.UseSocketActivation | 0 | (Linux only) Use systemd socket activation instead of binding own socket |

---

## REMOTE ACCESS

| Key | Default | Description |
|-----|---------|-------------|
| Ra.Enable | 0 | Enable remote console (telnet) |
| Ra.IP | "0.0.0.0" | Bind address for remote console |
| Ra.Port | 3443 | TCP port for remote console |
| Ra.MinLevel | 3 | Minimum account security level to use remote console |
| SOAP.Enabled | 0 | Enable the SOAP service |
| SOAP.IP | "127.0.0.1" | Bind address for SOAP service |
| SOAP.Port | 7878 | TCP port for SOAP service |

---

## CRYPTOGRAPHY

| Key | Default | Description |
|-----|---------|-------------|
| TOTPMasterSecret | (blank) | Key for decrypting TOTP secrets; required for `.account 2fa` commands |

---

## PERFORMANCE

| Key | Default | Description |
|-----|---------|-------------|
| ThreadPool | 2 | Global thread pool size (used for signals, RA, DB pings, freeze check, networking) |
| UseProcessors | 0 | CPU affinity bitmask; 0 = let OS decide |
| ProcessPriority | 1 | Process priority: 1=High, 0=Normal (Windows) |
| Compression | 1 | Client update packet compression level (1=speed, 9=best compression) |

---

## LOGGING

**Scalar logging keys:**

| Key | Default | Description |
|-----|---------|-------------|
| PacketLogFile | "" | Binary packet log filename (.pkt); empty = disabled |
| LogDB.Opt.ClearInterval | 10 | Minutes between clearing old `logs` DB table entries |
| LogDB.Opt.ClearTime | 1209600 | Seconds to keep `logs` table entries (default 14 days; 0=never clear) |
| RecordUpdateTimeDiffInterval | 300000 | Milliseconds between writing world-update diff to log (0=off) |
| MinRecordUpdateTimeDiff | 100 | Only log update diff when it exceeds this value (ms) |
| IPLocationFile | "" | Path to IP2Location CSV database; empty = disabled |
| AllowLoggingIPAddressesInDatabase | 1 | Allow IP addresses to be stored in the DB log |
| Allow.IP.Based.Action.Logging | 0 | Log player actions keyed by session IP |
| LogSpamReports | 1 | Log player spam reports (chat/mail/calendar) to the DB |
| Log.Async.Enable | 0 | Enable asynchronous log message writing |

**Appender keys** — format: `Type,LogLevel,Flags[,opt1,opt2,opt3]`  
Type: 0=none 1=console 2=file 3=DB. LogLevel: 0=off 1=fatal 2=error 3=warn 4=info 5=debug 6=trace. See conf.dist for flag and color values.

| Key | Default | Description |
|-----|---------|-------------|
| Appender.Console | 1,4,0,"1 9 3 6 5 8" | Console appender at Info level with color |
| Appender.Server | 2,5,0,Server.log,w | File appender writing Server.log (overwrite mode) |
| Appender.Playerbots | 2,5,0,Playerbots.log,w | File appender writing Playerbots.log (overwrite mode) |
| Appender.Errors | 2,2,0,Errors.log,w | File appender writing Errors.log at Error level only |

**Logger keys** — format: `LogLevel,AppenderList`. Many optional category loggers exist (chat, spells, entities, network, etc.); see conf.dist for the full commented list. Active defaults:

| Key | Default | Description |
|-----|---------|-------------|
| Logger.root | 2,Console Server | Root logger; catches everything not matched by a child logger |
| Logger.diff | 3,Console Server | World update diff warnings |
| Logger.mmaps | 4,Server | Movement map loading messages |
| Logger.scripts.hotswap | 4,Console Server | Hot-swap script reload messages |
| Logger.server | 4,Console Server | General server info messages |
| Logger.sql.sql | 2,Console Errors | SQL errors (routed to Errors.log) |
| Logger.sql.updates | 4,Console Server Errors | SQL update messages (also routed to Errors.log) |
| Logger.sql | 4,Console Server | General SQL messages |
| Logger.time.update | 4,Console Server | Time update messages |
| Logger.module | 4,Console Server | Module load/init messages |
| Logger.spells.scripts | 2,Console Errors | Spell script errors (routed to Errors.log) |
| Logger.playerbots | 5,Console Playerbots | Playerbot verbose log (routed to Playerbots.log) |

---

## METRIC

| Key | Default | Description |
|-----|---------|-------------|
| Metric.Enable | 0 | Enable statistics export to InfluxDB |
| Metric.InfluxDB.Connection | "127.0.0.1;8086;worldserver" | InfluxDB v1 connection string |
| Metric.InfluxDB.v2 | 0 | Enable InfluxDB v2 mode |
| Metric.InfluxDB.Org | "" | InfluxDB v2 organization |
| Metric.InfluxDB.Bucket | "" | InfluxDB v2 bucket |
| Metric.InfluxDB.Token | "" | InfluxDB v2 authentication token |
| Metric.Interval | 1 | Seconds between metric batch sends |
| Metric.OverallStatusInterval | 1 | Seconds between overall status data collection |

---

## SERVER

| Key | Default | Description |
|-----|---------|-------------|
| BirthdayTime | 1222964635 | Project birth date as Unix timestamp (used for server birthday event) |
| PlayerLimit | 1000 | Max players in world (0=unlimited); excludes GMs and admins |
| World.RealmAvailability | 1 | 1=realm open to players; 0=closed (character creation still allowed) |
| GameType | 0 | Realm type: 0/4=Normal, 1=PvP, 6=RP, 8=RPPVP, 16=FFA-PvP |
| RealmZone | 1 | Realm locale zone; controls allowed character-name character sets (see conf.dist for full list) |
| DBC.Locale | 255 | DBC language: 255=auto-detect, 0=EN, 1=KO, 2=FR, 3=DE, 4=ZH, 5=TW, 6=ES, 7=MX, 8=RU |
| Expansion | 2 | Allowed content: 0=Classic, 1=TBC, 2=WotLK |
| ClientCacheVersion | 0 | Cache-bust value for clients; 0=use DB value |
| SessionAddDelay | 10000 | Microseconds a network thread waits before adding a new session to the world map |
| CloseIdleConnections | 1 | Automatically disconnect idle connections |
| SocketTimeOutTime | 900000 | Milliseconds before disconnecting idle character-select connections (default 15 min) |
| SocketTimeOutTimeActive | 60000 | Milliseconds before disconnecting idle in-world connections (default 1 min) |
| MaxOverspeedPings | 2 | Ping count threshold before disconnecting a client for overspeed; 0=off |
| DisconnectToleranceInterval | 0 | Seconds a disconnected player can skip re-queue after reconnecting |
| EnableLoginAfterDC | 1 | Allow logging back into a character that is still in the world after a DC |
| MinWorldUpdateTime | 1 | Minimum milliseconds between world update ticks |
| UpdateUptimeInterval | 10 | Minutes between realm uptime record updates |
| MaxCoreStuckTime | 0 | Seconds before force-crashing a frozen server (0=disabled; use 30+) |
| SaveRespawnTimeImmediately | 1 | Save creature/object respawn times at death/use rather than at grid unload |
| Server.LoginInfo | 0 | Show core version in `.server info` on login |
| ShowKickInWorld | 0 | Broadcast server-wide message when a player is kicked |
| ShowMuteInWorld | 0 | Broadcast server-wide message when a player is muted |
| ShowBanInWorld | 0 | Broadcast server-wide message when a player is banned |
| MaxWhoListReturns | 49 | Max players shown in /who list |
| PreventAFKLogout | 0 | 0=AFK players can be logged out; 1=only protect sanctuaries; 2=protect everywhere |

---

## PACKET SPOOF PROTECTION SETTINGS

| Key | Default | Description |
|-----|---------|-------------|
| PacketSpoof.BanMode | 0 | Ban mode when PacketSpoof.Policy=2: 0=ban account, 1=ban IP |
| PacketSpoof.BanDuration | 86400 | Duration in seconds of a packet-spoof ban (0=permanent) |

---

## WARDEN

| Key | Default | Description |
|-----|---------|-------------|
| Warden.Enabled | 1 | Enable the Warden anti-cheat system |
| Warden.NumMemChecks | 3 | Memory checks sent per cycle (0=off) |
| Warden.NumLuaChecks | 1 | Lua checks sent per cycle (0=off) |
| Warden.NumOtherChecks | 7 | Other checks (module/hash) sent per cycle (0=off) |
| Warden.ClientResponseDelay | 600 | Seconds before disconnecting a non-responding client (0=never kick) |
| Warden.ClientCheckHoldOff | 30 | Seconds to wait before sending the next check to a client |
| Warden.ClientCheckFailAction | 0 | Action on check fail: 0=log only, 1=kick, 2=ban |
| Warden.BanDuration | 86400 | Seconds a Warden ban lasts (0=permanent) |

---

## AUTO BROADCAST

| Key | Default | Description |
|-----|---------|-------------|
| AutoBroadcast.On | 0 | Enable automatic chat broadcasts |
| AutoBroadcast.Center | 0 | Broadcast display method: 0=announce, 1=notify, 2=both |
| AutoBroadcast.Timer | 60000 | Milliseconds between auto-broadcasts |
| AutoBroadcast.MinDisableLevel | 0 | Min level to allow players to disable auto-broadcasts (0=no one can) |

---

## VISIBILITY AND DISTANCES

| Key | Default | Description |
|-----|---------|-------------|
| Visibility.GroupMode | 1 | Which group members can see invisible allies: 0=party, 1=raid, 2=faction |
| Visibility.Distance.Continents | 100 | Visibility range on continents in yards (max 250) |
| Visibility.Distance.Instances | 170 | Visibility range in instances in yards |
| Visibility.Distance.BGArenas | 250 | Visibility range in battlegrounds/arenas in yards |
| Visibility.ObjectSparkles | 1 | Show sparkles on quest-related game objects |
| Visibility.ObjectQuestMarkers | 1 | Show quest icons above game objects (post-2.3 behavior) |

---

## MAPS

| Key | Default | Description |
|-----|---------|-------------|
| MapUpdateInterval | 10 | Milliseconds between map update ticks |
| MapUpdate.Threads | 1 | Number of threads for map updates |
| MoveMaps.Enable | 1 | Enable pathfinding using movement maps (mmaps) |
| vmap.enableLOS | 1 | Enable line-of-sight checks using vmaps |
| vmap.enableHeight | 1 | Enable height calculation using vmaps |
| vmap.petLOS | 1 | Check line-of-sight for pet attacks to prevent shooting through walls |
| vmap.BlizzlikePvPLOS | 1 | Allow spells through BG/arena doodads (treestumps, etc.) as on retail |
| vmap.BlizzlikeLOSInOpenWorld | 1 | Allow spells through open-world objects (stumps, etc.) as on retail |
| vmap.enableIndoorCheck | 1 | Use vmaps to enforce outdoor-only auras (mounts, etc.) indoors |
| DetectPosCollision | 1 | Check final positions for collisions; 0=less CPU but less precision |
| CheckGameObjectLoS | 1 | Include dynamic game objects (doors, chests) in LoS checks |
| PreloadAllNonInstancedMapGrids | 0 | Preload all non-instanced grids at startup (~9 GB extra RAM) |
| DontCacheRandomMovementPaths | 0 | 0=cache random movement paths (more RAM); 1=recalculate (more CPU) |

---

## WEATHER

| Key | Default | Description |
|-----|---------|-------------|
| ActivateWeather | 1 | Enable the weather system |
| ChangeWeatherInterval | 600000 | Milliseconds between weather updates (default 10 min) |

---

## TICKETS

| Key | Default | Description |
|-----|---------|-------------|
| AllowTickets | 1 | Allow players to submit GM tickets |
| LevelReq.Ticket | 1 | Minimum level required to submit a ticket |
| DeletedCharacterTicketTrace | 0 | Keep ticket records when the submitting character is deleted |

---

## COMMAND

| Key | Default | Description |
|-----|---------|-------------|
| AllowPlayerCommands | 1 | Allow players to use server commands |
| Command.LookupMaxResults | 0 | Max results returned by `.lookup` commands (0=unlimited) |
| Die.Command.Mode | 1 | Prevent loot/death-event triggers from `.die` GM command |

---

## GAME MASTER

| Key | Default | Description |
|-----|---------|-------------|
| GM.LoginState | 2 | GM mode at login: 0=off, 1=on, 2=last saved state |
| GM.Visible | 2 | GM visibility at login: 0=invisible, 1=visible, 2=last saved state |
| GM.Chat | 2 | GM chat mode at login: 0=off, 1=on, 2=last saved state |
| GM.WhisperingTo | 2 | GM whisper acceptance at login: 0=off, 1=on, 2=last saved state |
| GM.InGMList.Level | 3 | Max GM level shown in GM list while not in GM mode (0=players only, 3=all) |
| GM.InWhoList.Level | 3 | Max GM level shown in /who list when visible (0=players only, 3=all) |
| GM.StartLevel | 1 | Starting level for GM characters |
| GM.AllowInvite | 0 | Allow players to invite GM characters to groups |
| GM.AllowFriend | 0 | Allow players to add GM characters to their friends list |
| GM.LowerSecurity | 0 | Allow lower security levels to run commands on higher-security characters |
| GM.TicketSystem.ChanceOfGMSurvey | 50 | Percentage chance a GM survey is sent after closing a ticket (0=off) |

---

## CHEAT

| Key | Default | Description |
|-----|---------|-------------|
| DisableWaterBreath | 4 | Min security level for free water breathing (4=off for everyone) |
| AllFlightPaths | 0 | Characters start with all flight paths (both factions) known |
| InstantFlightPaths | 0 | 0=normal flight time; 1=instant; 2=instant but player-toggleable |
| AlwaysMaxSkillForLevel | 0 | Auto-max all skills on login or level-up |
| AlwaysMaxWeaponSkill | 0 | Auto-max weapon and defense skills on login or level-up |
| PlayerStart.AllReputation | 0 | New characters start with high-level reputations already earned |
| PlayerStart.CustomSpells | 0 | Grant new characters spells from `playercreateinfo_spell_custom` |
| PlayerStart.MapsExplored | 0 | New characters start with all maps explored |
| InstantLogout | 1 | Min security level for instant logout (0=everyone, 4=nobody) |

---

## CHARACTER DATABASE

| Key | Default | Description |
|-----|---------|-------------|
| PlayerSaveInterval | 900000 | Milliseconds between automatic character saves (default 15 min) |
| PlayerSave.Stats.MinLevel | 0 | Min level to save character stats externally; 0=disabled |
| PlayerSave.Stats.SaveOnlyOnLogout | 1 | Save external character stats only on logout rather than every save |
| CleanCharacterDB | 0 | Remove deprecated achievements/skills/spells/talents on startup |
| PersistentCharacterCleanFlags | 0 | Bitmask of cleanup types that remain active after running (see conf.dist) |
| ValidateSkillLearnedBySpells | 1 | Remove invalid spells (wrong race/class) on load; disable at your own risk |

---

## CHARACTER DELETE

| Key | Default | Description |
|-----|---------|-------------|
| CharDelete.Method | 0 | 0=remove from DB; 1=unlink from account (appears deleted in-game) |
| CharDelete.MinLevel | 0 | Level threshold above which unlinking is used; 0=same method for all |
| CharDelete.KeepDays | 30 | Days to keep unlinked characters before purging (0=never delete) |

---

## CHARACTER CREATION

| Key | Default | Description |
|-----|---------|-------------|
| MinPlayerName | 2 | Minimum character name length (1–12) |
| MinPetName | 2 | Minimum pet name length (1–12) |
| DeclinedNames | 0 | Allow Russian clients to set declined name forms |
| StrictNames.Reserved | 1 | Block reserved names (from DBC) for players, pets, and charters |
| StrictNames.Profanity | 1 | Block profane names (from DBC) for players, pets, and charters |
| StrictPlayerNames | 0 | Character name charset enforcement (0=off, 1=basic Latin, 2=realm zone, 3=both) |
| StrictPetNames | 0 | Pet name charset enforcement (same values as StrictPlayerNames) |
| CharacterCreating.Disabled | 0 | Disable character creation by faction: 0=all, 1=Alliance, 2=Horde, 3=both |
| CharacterCreating.Disabled.RaceMask | 0 | Bitmask of races disabled for creation (see conf.dist for race values) |
| CharacterCreating.Disabled.ClassMask | 0 | Bitmask of classes disabled for creation (see conf.dist for class values) |
| CharactersPerAccount | 50 | Max characters per account across all realms |
| CharactersPerRealm | 10 | Max characters per account on this realm (1–10) |
| HeroicCharactersPerRealm | 1 | Max Death Knight characters per account on this realm |
| CharacterCreating.MinLevelForHeroicCharacter | 55 | Require another character of this level to create a DK (0=no restriction) |
| StartPlayerLevel | 1 | Starting level for new characters (1–MaxPlayerLevel) |
| StartHeroicPlayerLevel | 55 | Starting level for Death Knights |
| SkipCinematics | 0 | Skip intro cinematic: 0=show all, 1=show first per race, 2=skip all |
| StartPlayerMoney | 0 | Starting copper for new characters |
| StartHeroicPlayerMoney | 2000 | Starting copper for Death Knight characters (default 20 silver) |
| PlayerStart.String | "" | Message shown at first login of a new character; empty=disabled |

---

## CHARACTER

| Key | Default | Description |
|-----|---------|-------------|
| EnablePlayerSettings | 0 | Enable per-character settings storage |
| MaxPlayerLevel | 80 | Maximum player level (1–255; beyond 100 not recommended) |
| MinDualSpecLevel | 40 | Level requirement to unlock Dual Talent Specialization |
| WaterBreath.Timer | 180000 | Underwater breath timer in milliseconds (default 3 min) |
| EnableLowLevelRegenBoost | 1 | Greatly increase HP/mana regen for characters under level 15 (patch 3.3 behavior) |
| Rate.MoveSpeed.Player | 1 | Movement speed multiplier for players |
| Rate.MoveSpeed.NPC | 1 | Movement speed multiplier for NPCs |
| Rate.Damage.Fall | 1 | Fall damage multiplier |
| Rate.Talent | 1 | Talent point gain rate multiplier |
| Rate.Talent.Pet | 1 | Pet talent point gain rate multiplier |
| Rate.Health | 1 | Health regeneration rate multiplier |
| Rate.Mana | 1 | Mana regeneration rate multiplier |
| Rate.Rage.Income | 1 | Rage generation rate multiplier |
| Rate.Rage.Loss | 1 | Rage loss rate multiplier |
| Rate.RunicPower.Income | 1 | Runic power generation multiplier |
| Rate.RunicPower.Loss | 1 | Runic power loss multiplier |
| Rate.Focus | 1 | Focus regeneration multiplier |
| Rate.Energy | 1 | Energy regeneration multiplier |
| Rate.Loyalty | 1 | Pet loyalty gain multiplier |
| Rate.Rest.InGame | 1 | Rested XP accumulation rate while logged in |
| Rate.Rest.Offline.InTavernOrCity | 1 | Rested XP rate while logged out in a tavern or city |
| Rate.Rest.Offline.InWilderness | 1 | Rested XP rate while logged out in the wild |
| Rate.Rest.MaxBonus | 1.5 | Maximum rested XP bonus multiplier |
| Rate.MissChanceMultiplier.TargetCreature | 11 | Miss-chance formula multiplier when attacking a creature 3+ levels higher |
| Rate.MissChanceMultiplier.TargetPlayer | 7 | Miss-chance formula multiplier when attacking a player 3+ levels higher |
| Rate.MissChanceMultiplier.OnlyAffectsPlayer | 0 | 1=only affect player casters, not creature miss chance |
| LevelReq.Trade | 1 | Minimum level to initiate a trade |
| NoResetTalentsCost | 0 | Make talent respec free (1=free) |
| ToggleXP.Cost | 100000 | Copper cost to lock/unlock XP gain (default 10 gold) |
| SpellQueue.Enabled | 1 | Enable spell queue (queue next spell before current finishes) |
| SpellQueue.Window | 400 | Spell queue look-ahead window in milliseconds |

---

## ACHIEVEMENT

| Key | Default | Description |
|-----|---------|-------------|
| Achievement.RealmFirstKillWindow | 60 | Seconds after the first realm-first kill that other groups can still earn it (0=strict first-only) |

---

## SKILL

| Key | Default | Description |
|-----|---------|-------------|
| MaxPrimaryTradeSkill | 2 | Maximum primary professions a character can learn (0–11) |
| SkillChance.Prospecting | 0 | Allow skill-ups from prospecting (1=enabled) |
| SkillChance.Milling | 0 | Allow skill-ups from milling (1=enabled) |
| Rate.Skill.Discovery | 1 | Skill discovery chance multiplier |
| SkillGain.Crafting | 1 | Crafting skill gain rate |
| SkillGain.Defense | 1 | Defense skill gain rate |
| SkillGain.Gathering | 1 | Gathering skill gain rate |
| SkillGain.Weapon | 1 | Weapon skill gain rate |
| SkillChance.Orange | 100 | % chance to gain skill from an orange recipe |
| SkillChance.Yellow | 75 | % chance to gain skill from a yellow recipe |
| SkillChance.Green | 25 | % chance to gain skill from a green recipe |
| SkillChance.Grey | 0 | % chance to gain skill from a grey recipe |
| SkillChance.MiningSteps | 0 | Mining skill-up chance reduction per N skill points (0=off) |
| SkillChance.SkinningSteps | 0 | Skinning skill-up chance reduction per N skill points (0=off) |
| OffhandCheckAtSpellUnlearn | 1 | Re-check offhand weapon restrictions when a spell is unlearned |

---

## STATS

| Key | Default | Description |
|-----|---------|-------------|
| Stats.Limits.Enable | 0 | Enable stat cap enforcement for dodge/parry/block/crit |
| Stats.Limits.Dodge | 95.0 | Maximum dodge percentage |
| Stats.Limits.Parry | 95.0 | Maximum parry percentage |
| Stats.Limits.Block | 95.0 | Maximum block percentage |
| Stats.Limits.Crit | 95.0 | Maximum crit percentage |

---

## REPUTATION

| Key | Default | Description |
|-----|---------|-------------|
| Rate.Reputation.Gain | 1 | Global reputation gain multiplier |
| Rate.Reputation.LowLevel.Kill | 1 | Reputation from killing grey (low-level) creatures |
| Rate.Reputation.LowLevel.Quest | 1 | Reputation from low-level quest completion |
| Rate.Reputation.RecruitAFriendBonus | 0.1 | Extra reputation rate for Recruit-A-Friend pairs |
| Rate.Reputation.Gain.WSG | 1 | Additional reputation multiplier for Warsong Gulch |
| Rate.Reputation.Gain.AB | 1 | Additional reputation multiplier for Arathi Basin |
| Rate.Reputation.Gain.AV | 1 | Additional reputation multiplier for Alterac Valley |

---

## EXPERIENCE

| Key | Default | Description |
|-----|---------|-------------|
| MaxGroupXPDistance | 74 | Max yards from a creature kill for group members to receive XP |
| Rate.XP.Kill | 1 | XP multiplier for creature kills |
| Rate.XP.Quest | 1 | XP multiplier for quest completion |
| Rate.XP.Quest.DF | 1 | XP multiplier for Dungeon Finder quests only |
| Rate.XP.Explore | 1 | XP multiplier for exploration discoveries |
| Rate.XP.Pet | 1 | XP multiplier for pet kills |
| Rate.XP.BattlegroundKill{AV,WSG,AB,EOTS,SOTA,IC} | 1 | XP multiplier for BG kills per battleground (requires Battleground.GiveXPForKills=1) |
| Rate.XP.BattlegroundBonus | 1 | XP multiplier for BG objectives (flag captures, base assaults, etc.) |
| Rate.Pet.LevelXP | 0.05 | Multiplier for XP required to level a pet (lower = faster) |

---

## CURRENCY

| Key | Default | Description |
|-----|---------|-------------|
| MaxHonorPoints | 75000 | Maximum honor points a character can hold |
| MaxHonorPointsMoneyPerPoint | 0 | Copper per honor point for overflow conversion (0=disabled) |
| StartHonorPoints | 0 | Honor points characters start with at creation |
| HonorPointsAfterDuel | 0 | Honor points awarded to duel winner (0=off) |
| Rate.Honor | 1 | Honor gain rate multiplier |
| MaxArenaPoints | 10000 | Maximum arena points a character can hold |
| StartArenaPoints | 0 | Arena points characters start with at creation |
| Arena.LegacyArenaPoints | 0 | Use TBC arena point calculation for seasons 1–5 when rating ≤ 1500 |
| Rate.ArenaPoints | 1 | Global arena point gain multiplier |
| Rate.ArenaPoints2v2 | 0.76 | Arena point gain multiplier for 2v2 bracket |
| Rate.ArenaPoints3v3 | 0.88 | Arena point gain multiplier for 3v3 bracket |
| PvPToken.Enable | 0 | Award a token item for each honorable kill |
| PvPToken.MapAllowType | 4 | Where tokens are awarded: 1=BG+FFA, 2=FFA only, 3=BG only, 4=all maps |
| PvPToken.ItemID | 29434 | Item ID of the PvP token (default: Badge of Justice) |
| PvPToken.ItemCount | 1 | Number of tokens awarded per kill |

---

## DURABILITY

| Key | Default | Description |
|-----|---------|-------------|
| DurabilityLoss.InPvP | 0 | Apply durability loss on death in PvP |
| DurabilityLoss.OnDeath | 10 | Percentage durability lost on death |
| DurabilityLossChance.Damage | 0.5 | Chance per hit to lose durability on a worn item (lower = more frequent) |
| DurabilityLossChance.Absorb | 0.5 | Chance per absorbed-damage event to lose armor durability |
| DurabilityLossChance.Parry | 0.05 | Chance per parry to lose main-weapon durability |
| DurabilityLossChance.Block | 0.05 | Chance per block to lose shield durability |

---

## DEATH

| Key | Default | Description |
|-----|---------|-------------|
| Death.SicknessLevel | 11 | Level at which resurrection sickness starts applying |
| Death.CorpseReclaimDelay.PvP | 1 | Increase corpse reclaim delay on PvP deaths |
| Death.CorpseReclaimDelay.PvE | 1 | Increase corpse reclaim delay on PvE deaths |
| Death.Bones.World | 1 | Leave bones instead of a corpse after resurrection in the open world |
| Death.Bones.BattlegroundOrArena | 1 | Leave bones instead of a corpse after resurrection in BG/arena |

---

## PET

| Key | Default | Description |
|-----|---------|-------------|
| Pet.RankMod.Health | 1 | Apply rank health rate modifiers to pet HP |

---

## ITEM DELETE

| Key | Default | Description |
|-----|---------|-------------|
| ItemDelete.Method | 0 | 0=delete from DB immediately; 1=save to DB for recovery |
| ItemDelete.Vendor | 0 | Save items to DB when sold to a vendor |
| ItemDelete.Quality | 3 | Minimum item quality to save (0=grey, 3=blue, 4=purple, etc.) |
| ItemDelete.ItemLevel | 80 | Minimum item level to save |
| ItemDelete.KeepDays | 0 | Days to keep saved items before purging (0=keep forever) |

---

## ITEM

| Key | Default | Description |
|-----|---------|-------------|
| DBC.EnforceItemAttributes | 1 | 1=use DBC item attributes (ignore DB overrides); 0=use DB values |
| Rate.Drop.Item.Poor | 1 | Drop rate multiplier for grey (poor) items |
| Rate.Drop.Item.Normal | 1 | Drop rate multiplier for white (normal) items |
| Rate.Drop.Item.Uncommon | 1 | Drop rate multiplier for green (uncommon) items |
| Rate.Drop.Item.Rare | 1 | Drop rate multiplier for blue (rare) items |
| Rate.Drop.Item.Epic | 1 | Drop rate multiplier for purple (epic) items |
| Rate.Drop.Item.Legendary | 1 | Drop rate multiplier for orange (legendary) items |
| Rate.Drop.Item.Artifact | 1 | Drop rate multiplier for artifact items |
| Rate.Drop.Item.Referenced | 1 | Drop rate multiplier for referenced loot (shared tables) |
| Rate.Drop.Money | 1 | Gold drop rate multiplier |
| Rate.Drop.Item.ReferencedAmount | 1 | Multiplier for the amount of items from referenced loot (affects many raid bosses) |
| Rate.Drop.Item.GroupAmount | 1 | Multiplier for grouped item amounts (affects many dungeon bosses) |
| LootNeedBeforeGreedILvlRestriction | 70 | Need Before Greed: min iLvl restriction for items below player's subclass in DF groups (0=off) |
| Item.SetItemTradeable | 1 | Allow BoP items to be traded among raid members for 2 hours (0=disable) |

---

## QUEST

| Key | Default | Description |
|-----|---------|-------------|
| Quests.EnableQuestTracker | 0 | Store quest completion/abandonment data in DB for bug tracking |
| QuestPOI.Enabled | 1 | Show quest points of interest on the map |
| Quests.LowLevelHideDiff | 4 | Hide quests more than N levels below the player from the map marker |
| Quests.HighLevelHideDiff | 7 | Hide quests more than N levels above the player from the map marker |
| Quests.IgnoreRaid | 0 | Allow non-raid quests to be completed in a raid group |
| Quests.IgnoreAutoAccept | 0 | Force manual acceptance of all quests (ignore auto-accept flag) |
| Quests.IgnoreAutoComplete | 0 | Force manual completion of all quests (ignore auto-complete flag) |
| Rate.RewardQuestMoney | 1 | Multiplier for money rewarded by quests |
| Rate.RewardBonusMoney | 1 | Multiplier for bonus money rewarded at max level |

---

## CREATURE

| Key | Default | Description |
|-----|---------|-------------|
| MonsterSight | 50.0 | Max distance in yards for "monster" creature sight via CreatureAI::IsVisible |
| Rate.Creature.Aggro | 1 | Creature aggro radius multiplier |
| CreatureFamilyFleeAssistanceRadius | 30 | Yards a fleeing creature searches for assistance (0=off) |
| CreatureLeashRadius | 30 | Yards from pull position before a creature evades back (0=off) |
| CreatureFamilyAssistanceRadius | 10 | Yards for static creature assistance call (0=off) |
| CreatureFamilyAssistanceDelay | 2000 | Milliseconds before creature calls for assistance |
| CreatureFamilyAssistancePeriod | 3000 | Milliseconds between repeated assistance calls (0=off) |
| CreatureFamilyFleeDelay | 7000 | Milliseconds a creature can flee if no assistance was found |
| WorldBossLevelDiff | 3 | Level differential that classifies a creature as a world boss |
| Corpse.Decay.NORMAL | 60 | Seconds before a normal creature corpse decays (1 min) |
| Corpse.Decay.RARE | 300 | Seconds before a rare creature corpse decays (5 min) |
| Corpse.Decay.ELITE | 300 | Seconds before an elite creature corpse decays (5 min) |
| Corpse.Decay.RAREELITE | 300 | Seconds before a rare-elite corpse decays (5 min) |
| Corpse.Decay.WORLDBOSS | 3600 | Seconds before a world boss corpse decays (1 hour) |
| Rate.Corpse.Decay.Looted | 0.5 | Multiplier applied to corpse decay time after looting |
| Rate.Creature.Normal.Damage | 1 | Melee damage multiplier for normal creatures |
| Rate.Creature.Elite.Elite.Damage | 1 | Melee damage multiplier for elite creatures |
| Rate.Creature.Elite.RARE.Damage | 1 | Melee damage multiplier for rare creatures |
| Rate.Creature.Elite.RAREELITE.Damage | 1 | Melee damage multiplier for rare-elite creatures |
| Rate.Creature.Elite.WORLDBOSS.Damage | 1 | Melee damage multiplier for world bosses |
| Rate.Creature.Normal.SpellDamage | 1 | Spell damage multiplier for normal creatures |
| Rate.Creature.Elite.Elite.SpellDamage | 1 | Spell damage multiplier for elites |
| Rate.Creature.Elite.RARE.SpellDamage | 1 | Spell damage multiplier for rares |
| Rate.Creature.Elite.RAREELITE.SpellDamage | 1 | Spell damage multiplier for rare-elites |
| Rate.Creature.Elite.WORLDBOSS.SpellDamage | 1 | Spell damage multiplier for world bosses |
| Rate.Creature.Normal.HP | 1 | HP multiplier for normal creatures |
| Rate.Creature.Elite.Elite.HP | 1 | HP multiplier for elites |
| Rate.Creature.Elite.RARE.HP | 1 | HP multiplier for rares |
| Rate.Creature.Elite.RAREELITE.HP | 1 | HP multiplier for rare-elites |
| Rate.Creature.Elite.WORLDBOSS.HP | 1 | HP multiplier for world bosses |
| ListenRange.Say | 40 | Yards players can read creature/gameobject say messages |
| ListenRange.TextEmote | 40 | Yards players can read creature/gameobject emotes |
| ListenRange.Yell | 300 | Yards players can read creature/gameobject yell messages |
| Creature.RepositionAgainstNpcs | 1 | Enable circling/repositioning in NPC-vs-NPC combat (uses more CPU) |
| Creature.MovingStopTimeForPlayer | 180000 | Milliseconds a creature pauses movement after player interaction |
| WaypointMovementStopTimeForPlayer | 120 | Seconds a waypoint-movement creature waits after player interaction |
| NpcEvadeIfTargetIsUnreachable | 5 | Seconds before a creature evades when its target is unreachable |
| NpcRegenHPIfTargetIsUnreachable | 1 | Regenerate HP for raid creatures when their target is unreachable |
| NpcRegenHPTimeIfTargetIsUnreachable | 10 | Seconds before unreachable-target HP regen begins in raids |
| Creatures.CustomIDs | "190010,…" | Comma-separated custom NPC IDs with hardcoded gossip dialogs (skipped in DB validation) |

---

## VENDOR

| Key | Default | Description |
|-----|---------|-------------|
| Rate.SellValue.Item.Poor | 1 | Vendor sell-value multiplier for grey items |
| Rate.SellValue.Item.Normal | 1 | Vendor sell-value multiplier for white items |
| Rate.SellValue.Item.Uncommon | 1 | Vendor sell-value multiplier for green items |
| Rate.SellValue.Item.Rare | 1 | Vendor sell-value multiplier for blue items |
| Rate.SellValue.Item.Epic | 1 | Vendor sell-value multiplier for purple items |
| Rate.SellValue.Item.Legendary | 1 | Vendor sell-value multiplier for orange items |
| Rate.SellValue.Item.Artifact | 1 | Vendor sell-value multiplier for artifact items |
| Rate.SellValue.Item.Heirloom | 1 | Vendor sell-value multiplier for heirloom items |
| Rate.BuyValue.Item.Poor | 1 | Vendor buy-price multiplier for grey items |
| Rate.BuyValue.Item.Normal | 1 | Vendor buy-price multiplier for white items |
| Rate.BuyValue.Item.Uncommon | 1 | Vendor buy-price multiplier for green items |
| Rate.BuyValue.Item.Rare | 1 | Vendor buy-price multiplier for blue items |
| Rate.BuyValue.Item.Epic | 1 | Vendor buy-price multiplier for purple items |
| Rate.BuyValue.Item.Legendary | 1 | Vendor buy-price multiplier for orange items |
| Rate.BuyValue.Item.Artifact | 1 | Vendor buy-price multiplier for artifact items |
| Rate.BuyValue.Item.Heirloom | 1 | Vendor buy-price multiplier for heirloom items |
| Rate.RepairCost | 1 | Item repair cost multiplier |

---

## GROUP

| Key | Default | Description |
|-----|---------|-------------|
| LeaveGroupOnLogout.Enabled | 0 | Auto-remove player from group on logout (does not affect raids or LFG groups) |
| Group.Raid.LevelRestriction | 10 | Minimum level for raid group membership |
| Group.RandomRollMaximum | 1000000 | Maximum value for the client `/roll` command |

---

## INSTANCE

| Key | Default | Description |
|-----|---------|-------------|
| Instance.GMSummonPlayer | 0 | Allow GMs to summon non-GM players into instances (0=GM only) |
| Instance.IgnoreLevel | 0 | Ignore level requirements when entering instances |
| Instance.IgnoreRaid | 0 | Ignore raid-group requirements when entering instances |
| Instance.ResetTimeHour | 4 | Hour of day (0–23) for the daily global instance reset |
| Instance.ResetTimeRelativeTimestamp | 1135814400 | Reference timestamp for calculating 3-day/7-day reset schedules |
| Rate.InstanceResetTime | 1 | Multiplier for raid/heroic instance reset intervals |
| Instance.UnloadDelay | 1800000 | Milliseconds before an empty instance map is unloaded (0=keep until reset) |
| AccountInstancesPerHour | 5 | Max distinct instances a player can enter per hour |
| Instance.SharedNormalHeroicId | 1 | Force ICC and RS Normal/Heroic to share lockout IDs |
| DungeonAccessRequirements.PrintMode | 1 | How to display entry requirement failures: 0=none, 1=one at a time, 2=all detailed |
| DungeonAccessRequirements.PortalAvgIlevelCheck | 0 | Enforce average iLvl check at dungeon/raid portals |
| DungeonAccessRequirements.OptionalStringID | 0 | ID of an acore_strings message to append to access requirement messages (0=off) |

---

## DUNGEON AND BATTLEGROUND FINDER

| Key | Default | Description |
|-----|---------|-------------|
| JoinBGAndLFG.Enable | 0 | Allow simultaneous BG queue and LFG queue |
| DungeonFinder.OptionsMask | 5 | LFG feature bitmask: 1=dungeon finder, 2=raid browser, 4=seasonal bosses |
| LFG.Location.All | 0 | Allow LFG queuing from anywhere |
| LFG.MaxKickCount | 2 | Max kicks allowed in an LFG group (0=never; max 3) |
| LFG.KickPreventionTimer | 900 | Seconds a newly-joined player is protected from being kicked |
| DungeonAccessRequirements.LFGLevelDBCOverride | 0 | Use DB table `dungeon_access_requirements` levels to filter the LFG window |
| DungeonFinder.CastDeserter | 1 | Cast Deserter on players who leave a dungeon early |
| DungeonFinder.AllowCompleted | 1 | Allow already-completed heroics to be re-queued via LFG |
| DungeonFinder.DungeonSelectionCooldown | 0 | Minutes before the same dungeon can be assigned again after completion (0=off) |

---

## CHARTER

| Key | Default | Description |
|-----|---------|-------------|
| MinCharterName | 2 | Minimum guild/team charter name length (1–24) |
| StrictCharterNames | 0 | Charter name charset enforcement (0=off, 1=basic Latin, 2=realm zone, 3=both) |

---

## GUILD

| Key | Default | Description |
|-----|---------|-------------|
| Guild.EventLogRecordsCount | 100 | Number of guild event log entries to retain per guild |
| Guild.ResetHour | 6 | Hour of day for guild daily cap resets |
| Guild.BankEventLogRecordsCount | 25 | Number of guild bank event log entries to retain |
| MinPetitionSigns | 9 | Signatures required to create a guild (0–9) |
| Guild.CharterCost | 1000 | Copper cost of a guild charter (default 10 silver) |
| Guild.AllowMultipleGuildMaster | 0 | Allow multiple guild masters (set via `.guild rank`) |
| Guild.BankInitialTabs | 0 | Number of free guild bank tabs on creation (0–6) |
| Guild.BankTabCost0 | 1000000 | Cost in copper for guild bank tab 1 (default 100 gold) |
| Guild.BankTabCost1 | 2500000 | Cost in copper for guild bank tab 2 (250 gold) |
| Guild.BankTabCost2 | 5000000 | Cost in copper for guild bank tab 3 (500 gold) |
| Guild.BankTabCost3 | 10000000 | Cost in copper for guild bank tab 4 (1000 gold) |
| Guild.BankTabCost4 | 25000000 | Cost in copper for guild bank tab 5 (2500 gold) |
| Guild.BankTabCost5 | 50000000 | Cost in copper for guild bank tab 6 (5000 gold) |
| Guild.MemberLimit | 0 | Cap on guild members; 0=disabled |

---

## FFAPVP

| Key | Default | Description |
|-----|---------|-------------|
| FFAPvPTimer | 30 | Seconds before FFA-PvP flag drops after leaving a FFA zone |

---

## OUTDOORPVP

| Key | Default | Description |
|-----|---------|-------------|
| OutdoorPvPCaptureRate | 1.0 | Multiplier for outdoor PvP capture point progress rate |

---

## WINTERGRASP

| Key | Default | Description |
|-----|---------|-------------|
| Wintergrasp.Enable | 1 | 0=BG off (world still runs), 1=enabled, 2=all processing off |
| Wintergrasp.PlayerMax | 120 | Max players per team in Wintergrasp |
| Wintergrasp.PlayerMin | 0 | Minimum players per team to start the battle |
| Wintergrasp.PlayerMinLvl | 75 | Minimum level to participate |
| Wintergrasp.BattleTimer | 30 | Battle duration in minutes |
| Wintergrasp.NoBattleTimer | 150 | Minutes between battles (peace phase) |
| Wintergrasp.CrashRestartTimer | 10 | Minutes to delay Wintergrasp restart after a crash mid-battle |
| Wintergrasp.SkipBattleSessionCount | 3500 | If active sessions exceed this on peace expiry, war is skipped; 0=off |

---

## BATTLEGROUND

| Key | Default | Description |
|-----|---------|-------------|
| Battleground.PrepTime | 120 | Preparation phase duration in seconds (SOTA always uses 120) |
| Battleground.CastDeserter | 1 | Cast Deserter on players who leave a BG in progress |
| Battleground.QueueAnnouncer.Enable | 0 | Announce BG queue status to chat |
| Battleground.QueueAnnouncer.Limit.MinLevel | 0 | Only announce if level ≥ this value (0=no limit) |
| Battleground.QueueAnnouncer.Limit.MinPlayers | 3 | Only announce when at least this many players are queued (if MinLevel is set) |
| Battleground.QueueAnnouncer.SpamProtection.Delay | 30 | Seconds before re-announcing a player who rejoined queue |
| Battleground.QueueAnnouncer.PlayerOnly | 0 | 0=system message (all see it); 1=private (queued players only) |
| Battleground.QueueAnnouncer.Timed | 0 | Enable timer-based queue announcements |
| Battleground.QueueAnnouncer.Timer | 30000 | Milliseconds between timed queue announcements |
| Battleground.PrematureFinishTimer | 300000 | Milliseconds before a BG ends early when a team is too small (0=off) |
| Battleground.PremadeGroupWaitForMatch | 1800000 | Milliseconds a premade group waits for an opposing premade (0=off) |
| Battleground.GiveXPForKills | 0 | Award XP for honorable kills in battlegrounds |
| Battleground.Random.ResetHour | 6 | Hour of day for random BG reset |
| Battleground.StoreStatistics.Enable | 0 | Store BG scores in the database |
| Battleground.TrackDeserters.Enable | 0 | Track BG deserters in the database |
| Battleground.InvitationType | 0 | 0=fill as queued; 1=experimental balance; 2=force even teams |
| Battleground.ReportAFK.Timer | 4 | Minutes into battle before AFK reports are allowed |
| Battleground.ReportAFK | 3 | Number of AFK reports needed to kick a player (1–9) |
| Battleground.DisableQuestShareInBG | 0 | Disable quest sharing while in a BG |
| Battleground.DisableReadyCheckInBG | 0 | Disable ready checks while in a BG |
| Battleground.RewardWinnerHonorFirst | 30 | Honor multiplier for first random BG win (winner) |
| Battleground.RewardWinnerArenaFirst | 25 | Arena points for first random BG win (winner) |
| Battleground.RewardWinnerHonorLast | 15 | Honor multiplier for subsequent random BG wins (winner) |
| Battleground.RewardWinnerArenaLast | 0 | Arena points for subsequent random BG wins (winner) |
| Battleground.RewardLoserHonorFirst | 5 | Honor multiplier for first random BG loss |
| Battleground.RewardLoserHonorLast | 5 | Honor multiplier for subsequent random BG losses |
| Battleground.PlayerRespawn | 30 | BG player resurrection interval in seconds |
| Battleground.RestorationBuffRespawn | 20 | BG restoration buff respawn time in seconds |
| Battleground.BerserkingBuffRespawn | 120 | BG berserking buff respawn time in seconds |
| Battleground.SpeedBuffRespawn | 150 | BG speed buff respawn time in seconds |
| Battleground.Override.LowLevels.MinPlayers | 0 | Override minimum players per team for leveling BGs (0=off) |
| Battleground.Warsong.Flags | 3 | Flags required to win in Warsong Gulch |
| Battleground.Arathi.CapturePoints | 1600 | Score to win in Arathi Basin (WotLK: 1600; Vanilla: 2000) |
| Battleground.Alterac.Reinforcements | 600 | Total reinforcements per team in AV (requires server restart to change) |
| Battleground.Alterac.ReputationOnBossDeath | 350 | Reputation from AV boss kill (requires server restart to change) |
| Battleground.EyeOfTheStorm.CapturePoints | 1600 | Score to win in Eye of the Storm |

---

## ARENA

| Key | Default | Description |
|-----|---------|-------------|
| Arena.PrepTime | 60 | Arena preparation phase duration in seconds |
| Arena.MaxRatingDifference | 150 | Max team rating difference for rated matches (0=off) |
| Arena.RatingDiscardTimer | 600000 | Milliseconds before rating difference is ignored for matchmaking (0=off) |
| Arena.PreviousOpponentsDiscardTimer | 120000 | Milliseconds before previous opponents can be matched again (0=off) |
| Arena.AutoDistributePoints | 0 | Automatically distribute arena points on a schedule |
| Arena.AutoDistributeInterval | 7 | Days between automatic arena point distributions |
| Arena.GamesRequired | 10 | Arena matches required to qualify for point distribution |
| Arena.QueueAnnouncer.Enable | 0 | Announce arena queue status to chat |
| Arena.QueueAnnouncer.PlayerOnly | 0 | 0=system message; 1=queued players only |
| Arena.QueueAnnouncer.Detail | 3 | Announcement detail: 0=none, 1=rating, 2=name, 3=name+rating |
| Arena.ArenaStartRating | 0 | Starting team rating for season 6+ |
| Arena.LegacyArenaStartRating | 1500 | Starting team rating for seasons 1–5 |
| Arena.ArenaStartPersonalRating | 0 | Starting personal rating when joining a team |
| Arena.ArenaStartMatchmakerRating | 1500 | Starting matchmaker rating |
| Arena.ArenaWinRatingModifier1 | 48 | Rating gain modifier when winner rating is below 1300 |
| Arena.ArenaWinRatingModifier2 | 24 | Rating gain modifier when winner rating is 1300 or above |
| Arena.ArenaLoseRatingModifier | 24 | Rating loss modifier for the losing team |
| Arena.ArenaMatchmakerRatingModifier | 24 | Matchmaker rating change modifier |
| ArenaTeam.CharterCost.2v2 | 800000 | Copper cost to charter a 2v2 arena team (80 gold) |
| ArenaTeam.CharterCost.3v3 | 1200000 | Copper cost to charter a 3v3 arena team (120 gold) |
| ArenaTeam.CharterCost.5v5 | 2000000 | Copper cost to charter a 5v5 arena team (200 gold) |
| MaxAllowedMMRDrop | 500 | Max MMR drop from a player's peak (prevents MMR tanking exploit) |

---

## MAIL

| Key | Default | Description |
|-----|---------|-------------|
| MailDeliveryDelay | 3600 | Seconds of delivery delay when mailing items (default 1 hour) |
| LevelReq.Mail | 1 | Minimum level required to send or receive mail |

---

## TRANSPORT

| Key | Default | Description |
|-----|---------|-------------|
| IsContinentTransport.Enabled | 1 | Enable continent transports (ships, zeppelins, etc.) |
| IsPreloadedContinentTransport.Enabled | 0 | Preload transport maps at startup (uses ~2× RAM; not recommended on low-end) |

---

## CHAT CHANNEL

| Key | Default | Description |
|-----|---------|-------------|
| StrictChannelNames | 0 | Channel name charset enforcement (0=off, 1=basic Latin, 2=realm zone, 3=both) |
| AddonChannel | 1 | Enable addon channel traffic through the server |
| ChatFakeMessagePreventing | 1 | Collapse multiple whitespace runs to prevent fake chat (Blizzlike) |
| ChatStrictLinkChecking.Severity | 0 | Chat link validation: -1=color only, 0=data+color, 1=also verify link text |
| ChatStrictLinkChecking.Kick | 0 | 0=silently ignore invalid links; 1=kick the sender |
| ChatFlood.MessageCount | 10 | Messages per window before auto-muting a player (0=off) |
| ChatFlood.MessageDelay | 1 | Seconds between messages for flood counting |
| ChatFlood.AddonMessageCount | 100 | Addon messages per window before auto-muting (0=off) |
| ChatFlood.AddonMessageDelay | 1 | Seconds between addon messages for flood counting |
| ChatFlood.MuteTime | 10 | Seconds players are muted after triggering flood protection |
| Chat.MuteFirstLogin | 0 | Mute new players in public chat for a period after first login |
| Chat.MuteTimeFirstLogin | 120 | Minutes a new player is muted in public chat |
| Channel.RestrictedLfg | 1 | Restrict LFG channel to players registered in the LFG tool |
| Channel.SilentlyGMJoin | 0 | GMs join channels silently (no announcement); also silences GM kick/ban |
| Channel.ModerationGMLevel | 1 | Min GM security level for in-game channel moderator commands |
| ChatLevelReq.Channel | 1 | Minimum level to write in chat channels |
| ChatLevelReq.Whisper | 1 | Minimum level to whisper other players |
| ChatLevelReq.Say | 1 | Minimum level to use say/yell/emote |
| PartyLevelReq | 1 | Minimum level to invite non-friends to a group |
| PreserveCustomChannels | 0 | Persist custom channel settings (passwords, bans) in DB across restarts |
| PreserveCustomChannelDuration | 14 | Days before unused custom channels are purged from DB |

---

## FACTION INTERACTION

| Key | Default | Description |
|-----|---------|-------------|
| AllowTwoSide.Accounts | 1 | Allow both factions on the same account |
| AllowTwoSide.Interaction.Calendar | 0 | Allow cross-faction calendar invites |
| AllowTwoSide.Interaction.Chat | 0 | Allow cross-faction say chat |
| AllowTwoSide.Interaction.Channel | 0 | Allow cross-faction channel chat |
| AllowTwoSide.Interaction.Group | 0 | Allow cross-faction groups |
| AllowTwoSide.Interaction.Guild | 0 | Allow cross-faction guilds |
| AllowTwoSide.Interaction.Arena | 0 | Allow cross-faction arena teams |
| AllowTwoSide.Interaction.Auction | 0 | Allow cross-faction auction house access (all AHs become neutral) |
| TalentsInspecting | 1 | Allow inspecting opposite-faction characters' talents |
| ChangeFaction.MaxMoney | 0 | Max gold (in copper) allowed on a character for faction change; 0=disabled |

---

## RECRUIT A FRIEND

| Key | Default | Description |
|-----|---------|-------------|
| RecruitAFriend.MaxLevel | 60 | Max level up to which Recruit-A-Friend XP bonus applies |
| RecruitAFriend.MaxDifference | 4 | Max level difference between recruiter and friend for the XP bonus |
| MaxRecruitAFriendBonusDistance | 100 | Max yards from group members to receive the RaF XP bonus |

---

## CALENDAR

| Key | Default | Description |
|-----|---------|-------------|
| Calendar.DeleteOldEventsHour | 6 | Hour of day for daily deletion of expired calendar events |

---

## GAME EVENT

| Key | Default | Description |
|-----|---------|-------------|
| Event.Announce | 0 | Announce world event start/end to all players |

---

## WORLD STATE

| Key | Default | Description |
|-----|---------|-------------|
| Sunsreach.CounterMax | 10000 | Counter threshold to advance phases in the Sun's Reach Reclamation event |
| ScourgeInvasion.CounterFirst | 50 | First phase transition threshold for Scourge Invasion event |
| ScourgeInvasion.CounterSecond | 100 | Second phase transition threshold for Scourge Invasion event |
| ScourgeInvasion.CounterThird | 150 | Third phase transition threshold for Scourge Invasion event |

---

## AUCTION HOUSE

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouse.WorkerThreads | 1 | Number of auction house search worker threads |
| LevelReq.Auction | 1 | Minimum level required to use the auction house |
| Rate.Auction.Time | 1 | Auction duration multiplier |
| Rate.Auction.Deposit | 1 | Auction deposit cost multiplier |
| Rate.Auction.Cut | 1 | Auction house cut from sale price multiplier |

---

## PLAYER DUMP

| Key | Default | Description |
|-----|---------|-------------|
| PlayerDump.DisallowPaths | 1 | Prevent path characters in PlayerDump output filenames |
| PlayerDump.DisallowOverwrite | 1 | Prevent PlayerDump from overwriting existing files |

---

## CUSTOM

| Key | Default | Description |
|-----|---------|-------------|
| ICC.Buff.Horde | 73822 | Spell ID for the ICC strength-of-wrynn-equivalent buff (Horde); default = 30% |
| ICC.Buff.Alliance | 73828 | Spell ID for the ICC buff (Alliance); default = 30% |
| WipeGunshipBlizzlike.Enable | 1 | Wipe the gunship fight if no player is on deck (Blizzlike) |
| Minigob.Manabonk.Enable | 1 | Enable Minigob Manabonk world event NPC |
| Calculate.Creature.Zone.Area.Data | 0 | Recalculate creature zoneId/areaId on startup (WARNING: very slow) |
| Calculate.Gameoject.Zone.Area.Data | 0 | Recalculate gameobject zoneId/areaId on startup (WARNING: very slow) |
| DailyRBGArenaPoints.MinLevel | 71 | Minimum level to earn arena points from first daily RBG win |
| MunchingBlizzlike.Enabled | 1 | Use Blizzlike munching behavior for Rend/Ignite-style DoTs |
| Daze.Enabled | 1 | Enable mob melee attacks dazing the victim |
| InfiniteAmmo.Enabled | 0 | Disable ammo consumption for ranged/thrown attacks (0=Blizzlike) |

---

## DEBUG

| Key | Default | Description |
|-----|---------|-------------|
| Debug.Battleground | 0 | Enable 1v0 BG mode for testing (disables the in-game command) |
| Debug.Arena | 0 | Enable 1v1 arena mode for testing (disables the in-game command) |
| Debug.LFG | 0 | Enable single-player LFG queue for testing (disables the in-game command) |

---

## DYNAMIC RESPAWN SETTINGS

| Key | Default | Description |
|-----|---------|-------------|
| Respawn.DynamicRateCreature | 1 | Player count threshold for dynamic creature respawn scaling (1=off; higher value = more players needed before scaling kicks in) |
| Respawn.DynamicMinimumCreature | 10 | Minimum creature respawn time in seconds under dynamic scaling |
| Respawn.DynamicRateGameObject | 1 | Player count threshold for dynamic gameobject respawn scaling (1=off) |
| Respawn.DynamicMinimumGameObject | 10 | Minimum gameobject respawn time in seconds under dynamic scaling |
| Respawn.DynamicEscortNPC | 0 | Enable dynamic respawn behavior for escort quest NPCs |
| Respawn.ForceCompatibilityMode | 0 | Force all spawns to use legacy in-place respawn regardless of spawn group flags |

---

> For full comment text for any key, read the relevant section in `docs/configs/worldserver.conf.dist` directly.
