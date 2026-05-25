# GM Commands Reference

## How to Run Commands

**In-game (WoW client):** All commands require a leading dot, e.g. `.gm on`

**In worldserver console** (`docker attach ac-worldserver`): Leading dot is optional (both `.gm on` and `gm on` work). Use **Ctrl-P, Ctrl-Q** to detach — never Ctrl-C.

**Security levels:**
- `0` — Player (no GM access)
- `1` — Moderator
- `2` — Game Master
- `3` — Administrator
- `4` — Console only (commands marked 4 can only run from the worldserver console via `docker attach ac-worldserver`, not in-game)

Set GM level (from console):
```
account set gmlevel <account_name> <level> -1
# -1 means all realms. Example:
account set gmlevel mygm 3 -1
```

> For the full authoritative command list, read `docs/wikis/azerothcore-wiki/docs/gm-commands.md`.
> The table below covers the most commonly needed commands for everyday server management.

---

## Essential GM Commands

### Server / System

| Command | Security | Description |
|---------|----------|-------------|
| `.server info` | 0 | Show uptime, connections, diff time, map update time |
| `.server restart <delay>` | 3 | Schedule restart after N seconds |
| `.server shutdown <delay>` | 3 | Schedule shutdown after N seconds |
| `.server shutdown cancel` | 3 | Cancel scheduled shutdown/restart |
| `.server save` | 3 | Force save all player data |
| `.server reload config` | 3 | Reload config without restart (limited) |
| `.announce <message>` | 2 | Announce message to all players |
| `.notify <message>` | 1 | System notification to all players |

### Account Management

| Command | Security | Description |
|---------|----------|-------------|
| `account create <account> <password>` | 4 | Create a new account (console only — not usable in-game) |
| `account set gmlevel <account> <level> -1` | 4 | Set GM level (console only — not usable in-game) |
| `account set password <account> <pass> <pass>` | 4 | Set any account's password (console only) |
| `.account password <old> <new> <new>` | 0 | Change **your own** account password (in-game) |
| `.account lock` | 0 | Toggle account IP lock |
| `.account ban` / `.ban account` | 1 | Ban an account |
| `.ban character <name> <duration> <reason>` | 1 | Ban a character |
| `.unban account/character/ip` | 2 | Remove a ban |
| `.baninfo account/character/ip` | 2 | Show ban details |
| `.banlist account/character/ip` | 2 | List bans |

### Character / Player Management

| Command | Security | Description |
|---------|----------|-------------|
| `.character rename <name>` | 2 | Force character rename on next login |
| `.character level <name> <level>` | 2 | Set character level |
| `.levelup [levels]` | 2 | Level up selected target by N levels |
| `.character titles` | 2 | Manage character titles |
| `.cheat god [on/off]` | 3 | God mode for selected target |
| `.cheat casttime [on/off]` | 2 | Instant cast for selected target |
| `.cheat cooldown [on/off]` | 2 | No cooldowns |
| `.cheat power [on/off]` | 2 | Infinite power |
| `.modify hp <value>` | 2 | Set HP |
| `.modify mana <value>` | 2 | Set mana |
| `.modify money <amount>` | 2 | Add/remove money (copper; use 10000 = 1g) |
| `.modify xp <value>` | 2 | Add XP |
| `.revive` | 2 | Revive selected player |
| `.die` | 3 | Kill selected target |
| `.kick <name> [reason]` | 1 | Kick a player |
| `.mute <account> <duration> <reason>` | 1 | Mute an account |
| `.unmute <account>` | 1 | Unmute |

### Teleport / Position

| Command | Security | Description |
|---------|----------|-------------|
| `.tele <location>` | 1 | Teleport to named location |
| `.tele <name> <location>` | 2 | Teleport another player |
| `.tele add <name>` | 3 | Save current position as teleport point |
| `.tele del <name>` | 3 | Delete a teleport point |
| `.tele list` | 1 | List teleport locations |
| `.go creature <entry>` | 2 | Go to nearest creature by entry ID |
| `.go gameobject <guid>` | 2 | Go to gameobject by GUID |
| `.go graveyard <id>` | 2 | Go to graveyard |
| `.go xyz <x> <y> <z> [mapid]` | 2 | Teleport to exact coordinates |
| `.go zonexy <x> <y> [zone]` | 2 | Teleport to zone-local coordinates |
| `.groupgo <name>` | 2 | Teleport your group to another player |
| `.recall [name]` | 2 | Teleport player back to their recall point |
| `.saverecall` | 2 | Save current position as recall point |

### Item Management

| Command | Security | Description |
|---------|----------|-------------|
| `.additem <itemid/name> [count]` | 2 | Add item to selected player |
| `.additemset <setid>` | 2 | Add item set |
| `.item restore <GUID>` | 2 | Restore deleted item |
| `.item move <slot> <bag> <slot>` | 2 | Move item to slot |

> Item IDs: search Wowhead 3.3.5 or query `acore_world.item_template` WHERE name LIKE '%itemname%'

### Spell / Aura

| Command | Security | Description |
|---------|----------|-------------|
| `.spell learn <spellid>` | 2 | Teach spell to selected target |
| `.spell unlearn <spellid>` | 2 | Remove spell from selected target |
| `.aura <spellid>` | 2 | Apply aura to selected target |
| `.unaura <spellid>` | 2 | Remove aura from selected target |
| `.cast <spellid> [triggered]` | 2 | Cast spell at selected target |
| `.lookup spell <name>` | 2 | Search spells by name |

### Creature / NPC

| Command | Security | Description |
|---------|----------|-------------|
| `.npc add <entry>` | 2 | Spawn creature |
| `.npc delete` | 2 | Delete selected creature spawn |
| `.npc info` | 2 | Show selected NPC info |
| `.npc near [distance]` | 2 | List nearby NPCs |
| `.lookup creature <name>` | 2 | Search creature entries |
| `.wp show on` | 2 | Show waypoints of selected creature |

### Quest Management

| Command | Security | Description |
|---------|----------|-------------|
| `.quest add <questid>` | 2 | Add quest to selected player |
| `.quest remove <questid>` | 2 | Remove quest from selected player |
| `.quest complete <questid>` | 2 | Complete quest for selected player |
| `.quest reward <questid>` | 2 | Reward quest without completing objectives |
| `.lookup quest <name>` | 2 | Search quests by name |

### GM Mode

| Command | Security | Description |
|---------|----------|-------------|
| `.gm on` / `.gm off` | 1 | Toggle GM mode (invisible to players) |
| `.gm visible on/off` | 1 | Toggle GM visibility |
| `.gm fly on/off` | 2 | Toggle fly mode |
| `.gm list` | 2 | List online GMs |

### Debug / Info

| Command | Security | Description |
|---------|----------|-------------|
| `.debug setlevel <level>` | 3 | Set selected target's level |
| `.debug anim <animid>` | 3 | Play animation |
| `.debug send error <msgid>` | 3 | Send error message to client |
| `.lookup item <name>` | 1 | Search item templates by name |
| `.lookup map <name>` | 1 | Search maps by name |
| `.lookup player ip/email/account` | 2 | Find player by IP/email/account |
| `.player info <name>` | 1 | Show player info |
| `.account info <name>` | 2 | Show account info |

### Reload Commands (useful after DB edits)

| Command | Security | Description |
|---------|----------|-------------|
| `.reload all_scripts` | 3 | Reload all scripts |
| `.reload creature_template` | 3 | Reload creature templates |
| `.reload item_template` | 3 | Reload item templates |
| `.reload quest_template` | 3 | Reload quest templates |
| `.reload smart_scripts` | 3 | Reload SAI scripts |

---

## Useful Multi-Step Procedures

### Create a GM account (from console after first run)
```
account create mygm strongpassword
account set gmlevel mygm 3 -1
```

### Teleport a stuck player
```
# In-game, target the player:
.tele <location>
# Or bring them to you:
.summon <playername>
```

### Give yourself a specific item
```
.additem 19019          # Thunderfury (example; use actual item ID)
.additem "Thunderfury"  # Can also search by name
```

### Reset an instance for a group
```
.instance unbind <mapid> <difficulty>
```
