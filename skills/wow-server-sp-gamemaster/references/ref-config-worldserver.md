# worldserver.conf and AC_* Environment Variables

## How Configuration Works

AzerothCore reads config in this precedence order (highest wins):
1. **Environment variables** (`AC_*` in docker-compose files)
2. **.conf files** (`worldserver.conf`, `playerbots.conf`, etc.)
3. **Core defaults**

For this Docker-based install:
- `docker-compose.override.yml` holds all static AC_* env vars (source of truth for tuning)
- `docker-compose.admin.yml` holds admin-UI-written overrides (LAST precedence, merged after override.yml)
- The `.conf` files inside the container are the `.conf.dist` copies; **env vars win**

> **Never edit files you don't need to.** The only `.conf` file the installer edits post-install is `configs/modules/mod_ahbot.conf` (for GUIDs).

## AC_* Environment Variable Derivation Rule

AzerothCore converts config keys to env var names by:
1. Prefixing with `AC_`
2. Inserting underscore at lowercase→uppercase and letter→digit transitions
3. Replacing `.`, ` `, `-` with `_`
4. Uppercasing everything

**Examples:**
| Config Key | Env Var |
|-----------|---------|
| `AiPlayerbot.Enabled` | `AC_AI_PLAYERBOT_ENABLED` |
| `AiPlayerbot.MinRandomBots` | `AC_AI_PLAYERBOT_MIN_RANDOM_BOTS` |
| `MapUpdate.Threads` | `AC_MAP_UPDATE_THREADS` |
| `SkillGain.Crafting` | `AC_SKILL_GAIN_CRAFTING` |
| `Respawn.DynamicRateGameObject` | `AC_RESPAWN_DYNAMIC_RATE_GAME_OBJECT` |
| `GM.InGMList.Level` | `AC_GM_IN_GMLIST_LEVEL` |

**Unknown env vars are silently ignored** — always verify the key exists in the relevant `.conf.dist` under `docs/configs/`.

### Checking if an env var is loaded

```bash
# Verify all managed AC_* vars are present in the running worldserver:
docker exec ac-worldserver env | grep AC_

# Check a specific key:
docker exec ac-worldserver env | grep AC_AI_PLAYERBOT
```

## Where to Edit Config

**For permanent changes:** Edit `docker-compose.override.yml` and restart the worldserver:
```bash
cd /opt/stacks/azerothcore
docker compose restart ac-worldserver
```

**For admin-UI changes:** Use the Settings page in the admin web app. It writes to `docker-compose.admin.yml`.

> Do NOT edit `docker-compose.override.yml` directly if you want the admin app to manage the setting — it won't know about your manual edits and may overwrite them with a rollback.

## Key worldserver.conf Settings (via env vars)

### Core Performance
| Env Var | Default | Recommended (this server) | Description |
|---------|---------|--------------------------|-------------|
| `AC_MAP_UPDATE_THREADS` | `1` | `4` | Map update threads (set to CPU cores - 2, max 6) |
| `AC_MAP_UPDATE_INTERVAL` | `100` | `10` | Update interval in ms |
| `AC_MIN_WORLD_UPDATE_TIME` | `1` | `1` | Minimum world update time ms |
| `AC_PLAYER_LIMIT` | `100` | `0` | Max players (0=unlimited, needed for bots) |
| `AC_PRELOAD_ALL_NON_INSTANCED_MAP_GRIDS` | `0` | `0` | Preload all map grids (use 0 with bots) |
| `AC_SET_ALL_CREATURES_WITH_WAYPOINT_MOVEMENT_ACTIVE` | `0` | `0` | Keep 0 for performance |
| `AC_DONT_CACHE_RANDOM_MOVEMENT_PATHS` | `0` | `0` | Keep 0 for performance |

### XP Rates
| Env Var | Config Key | Description |
|---------|-----------|-------------|
| `AC_RATE_XP_KILL` | `Rate.XP.Kill` | XP from kills |
| `AC_RATE_XP_QUEST` | `Rate.XP.Quest` | XP from quests |
| `AC_RATE_XP_EXPLORE` | `Rate.XP.Explore` | XP from exploration |
| `AC_RATE_XP_PET` | `Rate.XP.Pet` | Pet XP |
| `AC_RATE_MONEY_QUEST` | `Rate.Money.Quest` | Quest money rewards |
| `AC_RATE_REPUTATION_GAIN` | `Rate.Reputation.Gain` | Reputation gain |
| `AC_SKILL_GAIN_CRAFTING` | `SkillGain.Crafting` | Crafting skill gain |
| `AC_SKILL_GAIN_GATHERING` | `SkillGain.Gathering` | Gathering skill gain |
| `AC_SKILL_GAIN_WEAPON` | `SkillGain.Weapon` | Weapon skill gain |
| `AC_SKILL_GAIN_DEFENSE` | `SkillGain.Defense` | Defense skill gain |

### Respawn
| Env Var | Description |
|---------|-------------|
| `AC_RESPAWN_DYNAMIC_MODE` | `0`=off, `1`=dynamic respawn scaling |
| `AC_RESPAWN_DYNAMIC_RATE_CREATURE` | Dynamic respawn rate multiplier for creatures |
| `AC_RESPAWN_DYNAMIC_RATE_GAME_OBJECT` | Dynamic respawn rate multiplier for game objects |
| `AC_RESPAWN_DYNAMIC_MINIMUM_CREATURE` | Minimum respawn time floor for creatures |

### Quests
| Env Var | Description |
|---------|-------------|
| `AC_QUESTS_IGNORE_AUTO_ACCEPT` | Set to `1` — bots need this to pick up quests correctly |

### PvP
| Env Var | Description |
|---------|-------------|
| `AC_PVP_TOKEN_ENABLE` | Enable PvP tokens |
| `AC_ALLOW_TWO_SIDE_INTERACTION_GROUP` | Allow cross-faction groups |
| `AC_ALLOW_TWO_SIDE_INTERACTION_GUILD` | Allow cross-faction guilds |
| `AC_ALLOW_TWO_SIDE_WHO_LIST` | Show both factions in /who |

### Instances
| Env Var | Description |
|---------|-------------|
| `AC_INSTANCE_SHARE_PLAYER_IP` | Share instance saves across account |
| `AC_RATE_INSTANCE_RESET_TIME` | Instance reset time multiplier |

## MySQL Tuning (`configs/mysql/custom.cnf`)

This file is created by the installer. Key settings:
```ini
[mysqld]
innodb_buffer_pool_size = 8G          # ~50% of RAM; set by installer based on available RAM
innodb_buffer_pool_instances = 8      # Derived as buffer_pool_size_in_GB (never persisted, always recomputed)
innodb_io_capacity = 500
innodb_io_capacity_max = 2500
innodb_use_fdatasync = ON
innodb_log_buffer_size = 32M
binlog_expire_logs_seconds = 432000   # 5 days
transaction_isolation = READ-COMMITTED
```

> Changing `innodb_buffer_pool_size` requires a **MySQL container restart**:
> ```bash
> docker restart ac-database
> ```

## docker-compose.override.yml Structure

The installer creates this file in Phase 2.5 with a heredoc. It contains:
- All static AC_* env vars
- All prompt-substituted values (playerbot count, map threads, PvP, XP rates)
- Only managed env vars (unknown keys are silently dropped by AC)

**Never edit this file to add env vars that don't map to real AC config keys.** Verify new keys in `docs/configs/*.conf.dist`.

## docker-compose.admin.yml

- Created empty by the admin installer
- Populated only by the admin UI's Apply flow
- LAST precedence (overrides override.yml)
- The admin app verifies each AC_* before writing (checks that it maps to a real config key)
- Snapshots before every write to `/opt/stacks/azerothcore-admin/snapshots/`

## Cross-Faction Settings

These keys allow the two factions to interact. All default to `0` (disabled). Set via `AC_*` env vars.

| Config Key | Env Var | Description |
|-----------|---------|-------------|
| `AllowTwoSide.Accounts` | `AC_ALLOW_TWO_SIDE_ACCOUNTS` | Allow same account to have both-faction chars (default: 1) |
| `AllowTwoSide.Interaction.Chat` | `AC_ALLOW_TWO_SIDE_INTERACTION_CHAT` | Cross-faction /say, /yell |
| `AllowTwoSide.Interaction.Channel` | `AC_ALLOW_TWO_SIDE_INTERACTION_CHANNEL` | Cross-faction channels |
| `AllowTwoSide.Interaction.Group` | `AC_ALLOW_TWO_SIDE_INTERACTION_GROUP` | Cross-faction parties and raids |
| `AllowTwoSide.Interaction.Guild` | `AC_ALLOW_TWO_SIDE_INTERACTION_GUILD` | Cross-faction guilds |
| `AllowTwoSide.Interaction.Arena` | `AC_ALLOW_TWO_SIDE_INTERACTION_ARENA` | Cross-faction arena teams |
| `AllowTwoSide.Interaction.Auction` | `AC_ALLOW_TWO_SIDE_INTERACTION_AUCTION` | Shared auction house |
| `AllowTwoSide.Interaction.Calendar` | `AC_ALLOW_TWO_SIDE_INTERACTION_CALENDAR` | Cross-faction calendar invites |

> For a mixed-faction bot setup where bots of both factions group with real players, you need at minimum `AllowTwoSide.Interaction.Group = 1`.

## Drop Rates

Set via `AC_*` env vars. All default to `1` (100%).

| Config Key | Env Var | Description |
|-----------|---------|-------------|
| `Rate.Drop.Item.Poor` | `AC_RATE_DROP_ITEM_POOR` | Gray item drop rate multiplier |
| `Rate.Drop.Item.Normal` | `AC_RATE_DROP_ITEM_NORMAL` | White item drop rate multiplier |
| `Rate.Drop.Item.Uncommon` | `AC_RATE_DROP_ITEM_UNCOMMON` | Green item drop rate multiplier |
| `Rate.Drop.Item.Rare` | `AC_RATE_DROP_ITEM_RARE` | Blue item drop rate multiplier |
| `Rate.Drop.Item.Epic` | `AC_RATE_DROP_ITEM_EPIC` | Purple item drop rate multiplier |
| `Rate.Drop.Item.Legendary` | `AC_RATE_DROP_ITEM_LEGENDARY` | Orange item drop rate multiplier |
| `Rate.Drop.Money` | `AC_RATE_DROP_MONEY` | Gold drop rate multiplier |
| `Rate.RewardQuestMoney` | `AC_RATE_REWARD_QUEST_MONEY` | Quest gold reward multiplier |

## Rest and Honor Rates

| Config Key | Env Var | Description |
|-----------|---------|-------------|
| `Rate.Rest.InGame` | `AC_RATE_REST_IN_GAME` | Rested XP accumulation rate while logged in at an inn |
| `Rate.Rest.Offline.InTavernOrCity` | `AC_RATE_REST_OFFLINE_IN_TAVERN_OR_CITY` | Rested XP accumulation while offline in a city/inn |
| `Rate.Rest.Offline.InWilderness` | `AC_RATE_REST_OFFLINE_IN_WILDERNESS` | Rested XP accumulation while offline in the wild |
| `Rate.Honor` | `AC_RATE_HONOR` | Honor points gain multiplier |
| `Rate.ArenaPoints` | `AC_RATE_ARENA_POINTS` | Arena points gain multiplier |

## Instance Settings

| Config Key | Env Var | Description |
|-----------|---------|-------------|
| `Rate.InstanceResetTime` | `AC_RATE_INSTANCE_RESET_TIME` | Instance lockout duration multiplier (1 = normal, 0.5 = half the reset time) |
| `Instance.ResetTimeHour` | `AC_INSTANCE_RESET_TIME_HOUR` | Hour of day when daily instances reset (default: 4, i.e. 4 AM) |
| `Instance.IgnoreLevel` | `AC_INSTANCE_IGNORE_LEVEL` | Allow entering instances below minimum level |
| `Instance.IgnoreRaid` | `AC_INSTANCE_IGNORE_RAID` | Allow entering raid instances without a raid group |
| `Instance.GMSummonPlayer` | `AC_INSTANCE_GM_SUMMON_PLAYER` | Allow GM to summon players into instances |
| `Instance.UnloadDelay` | `AC_INSTANCE_UNLOAD_DELAY` | Ms before an empty instance is unloaded from memory (default: 1800000 = 30 min) |

## Restarting the Server

```bash
cd /opt/stacks/azerothcore
# Graceful restart (via Docker):
docker restart ac-worldserver

# Full stack restart:
docker compose restart

# After editing override.yml:
docker compose up -d --force-recreate ac-worldserver
```

> The **admin app** provides a Restart button on the dashboard that handles graceful shutdown (announce → saveall → stop → wait → start).
