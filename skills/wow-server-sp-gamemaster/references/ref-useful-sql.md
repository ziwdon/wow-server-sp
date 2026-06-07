# Useful SQL Reference

## Connecting to the Database

```bash
# Interactive MySQL session:
docker exec -it ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD"

# One-shot query:
docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" \
    -e "SELECT COUNT(*) FROM acore_characters.characters WHERE online=1"
```

Key databases:
| Database | Contains |
|----------|---------|
| `acore_auth` | Accounts, realmlist, RBAC |
| `acore_characters` | Characters, items, quests, reputation |
| `acore_world` | World data (creatures, quests, items, spells) |
| `acore_playerbots` | Playerbot state and random bot data |

---

## Player / Account Queries

### Find a player by name
```sql
SELECT guid, name, account, level, class, race, online
FROM acore_characters.characters
WHERE name = 'CharacterName';
```

### Find account by character name
```sql
SELECT a.id, a.username, a.email, a.gmlevel
FROM acore_auth.account a
JOIN acore_characters.characters c ON c.account = a.id
WHERE c.name = 'CharacterName';
```

### List online players
```sql
SELECT name, level, class, race, zone
FROM acore_characters.characters
WHERE online = 1
ORDER BY name;
```

### Check playerbot pool size
```sql
SELECT COUNT(*) AS rndbot_chars
FROM acore_characters.characters c
JOIN acore_auth.account a ON a.id = c.account
WHERE a.username LIKE 'RNDBOT%';
-- Expected: configured_accounts × chars_per_account
-- Check configured bot count: grep AC_AI_PLAYERBOT_MIN_RANDOM_BOTS /opt/stacks/azerothcore/docker-compose.override.yml
```

### Check random bots by class and level
```sql
SELECT c.class, c.level, COUNT(*) as count
FROM acore_characters.characters c
JOIN acore_auth.account a ON a.id = c.account
WHERE a.username LIKE 'RNDBOT%'
GROUP BY c.class, c.level
ORDER BY c.class, c.level;
```

---

## Item Queries

### Find item by name
```sql
SELECT entry, name, Quality, ItemLevel, RequiredLevel
FROM acore_world.item_template
WHERE name LIKE '%Thunderfury%';
```

### Find what creatures drop an item
```sql
SET @ITEMID := 19019;  -- Replace with actual item ID

SELECT ct.name, clt.chance, ct.maxlevel
FROM acore_world.creature_template ct
JOIN acore_world.creature_loot_template clt ON ct.lootid = clt.entry
WHERE clt.item = @ITEMID;
```

### Find which reference loot tables contain an item
```sql
SET @ITEMID = 19019;

SELECT rlt.entry
FROM acore_world.reference_loot_template rlt
WHERE rlt.item = @ITEMID;
```

### Find items in a player's inventory
```sql
SELECT it.name, ci.bag, ci.slot, ci.count
FROM acore_characters.character_inventory ci
JOIN acore_world.item_template it ON ci.item_template = it.entry
WHERE ci.guid = <CHARACTER_GUID>
ORDER BY ci.bag, ci.slot;
```

---

## Quest Queries

### Find quest by name
```sql
SELECT entry, Title, MinLevel, QuestLevel, Type, RequiredClasses
FROM acore_world.quest_template
WHERE Title LIKE '%Onyxia%';
```

### Check quest status for a character
```sql
SELECT qt.Title, qs.status, qs.rewarded
FROM acore_characters.character_queststatus qs
JOIN acore_world.quest_template qt ON qt.entry = qs.quest
WHERE qs.guid = <CHARACTER_GUID>;
```

---

## Creature Queries

### Find creature by name
```sql
SELECT entry, name, minlevel, maxlevel, faction, unit_class
FROM acore_world.creature_template
WHERE name LIKE '%Ragnaros%';
```

### Find creature spawn by GUID
```sql
SET @CGUID := 12345;

SELECT ct.entry, ct.name, ct.minlevel, ct.maxlevel
FROM acore_world.creature_template ct
JOIN acore_world.creature c ON ct.entry = c.id
WHERE c.guid = @CGUID;
```

---

## Auction House Queries

### Check AH bot characters
```sql
SELECT c.guid, c.name, c.level, c.class, c.race
FROM acore_characters.characters c
JOIN acore_auth.account a ON a.id = c.account
WHERE a.username = 'ahbot';
```

### Count items in auction house
```sql
SELECT COUNT(*) AS total_listings,
       COUNT(DISTINCT itemEntry) AS distinct_items
FROM acore_characters.auctionhouse;
```

---

## Individual Progression Queries

### Check players' progression hidden quest rows
```sql
SELECT
  c.guid,
  c.name,
  COALESCE(MAX(q.quest - 66000), 0) AS progression_state,
  GROUP_CONCAT(q.quest ORDER BY q.quest) AS hidden_progression_quests
FROM acore_characters.characters c
LEFT JOIN acore_characters.character_queststatus_rewarded q
  ON q.guid = c.guid
 AND q.active = 1
 AND q.quest BETWEEN 66000 AND 66018
GROUP BY c.guid, c.name
ORDER BY c.name;
```

### Promote an offline player forward to an expansion boundary

This inserts hidden rewarded quests and deletes nothing. Use only for real,
offline player characters, and only for upward promotion. Boundary targets:
Vanilla = `0`, TBC = `8`, WotLK = `13`.

```sql
-- Example: promote character 12345 forward to TBC boundary state 8.
-- First confirm the character is offline and not already above the target:
SELECT
  c.guid,
  c.name,
  c.online,
  COALESCE(MAX(q.quest - 66000), 0) AS current_state
FROM acore_characters.characters c
LEFT JOIN acore_characters.character_queststatus_rewarded q
  ON q.guid = c.guid
 AND q.active = 1
 AND q.quest BETWEEN 66000 AND 66018
WHERE c.guid = 12345
GROUP BY c.guid, c.name, c.online;

-- Then insert missing hidden rewarded quests through the target state:
INSERT IGNORE INTO acore_characters.character_queststatus_rewarded (guid, quest, active)
VALUES
  (12345, 66001, 1),
  (12345, 66002, 1),
  (12345, 66003, 1),
  (12345, 66004, 1),
  (12345, 66005, 1),
  (12345, 66006, 1),
  (12345, 66007, 1),
  (12345, 66008, 1);
```

---

## Database Maintenance

### Check database sizes
```sql
SELECT table_schema AS 'Database',
       ROUND(SUM(data_length + index_length) / 1024 / 1024, 1) AS 'Size (MB)'
FROM information_schema.tables
GROUP BY table_schema
ORDER BY SUM(data_length + index_length) DESC;
```

### Check uptime record
```sql
SELECT * FROM acore_auth.uptime ORDER BY starttime DESC LIMIT 5;
```

### Recent bans
```sql
SELECT bandate, unbandate, bannedby, banreason, active
FROM acore_auth.account_banned
WHERE active = 1
ORDER BY bandate DESC;
```

---

## Backup / Restore

The canonical backup mechanism is the shared `scripts/backup.sh` (run by the nightly
cron and bundled into the admin image for the **Backups** page). Each run produces ONE
consolidated archive: `azerothcore-backup-<label>-<stamp>.tar.gz` containing
`manifest.json`, all four DB dumps under `sql/`, and staged configs under `config/`.
Labels: `daily` (cron), `manual` (Backups page), `prerestore` (auto-taken before an
in-app restore). The nightly daily run prunes every archive older than 7 days.

### Create a backup
```bash
# Host (daily mode — exactly what the cron runs; also prunes >7-day-old archives):
/opt/stacks/azerothcore/backup.sh
# Or: admin Backups page -> "Create backup" (manual label).
```

### Restore a backup
- **Same machine (rollback):** admin **Backups** page -> select an archive -> "Restore
  selected". Imports the DBs via `docker exec`, restores `docker-compose.admin.yml`, and
  takes a `prerestore` safety backup first.
- **Fresh machine (disaster recovery):** reinstall AzerothCore, copy the archive over,
  then `./scripts/restore-azerothcore.sh /path/to/archive.tar.gz` (preserves the fresh
  `.env` + `configs/mysql/custom.cnf` and re-fixes realmlist; see
  `docs/runbooks/disaster-recovery.md`).

### Ad-hoc single-database dump (one-off — NOT the cron/admin format)
For a quick throwaway dump of a single DB (e.g. before a destructive query below), not a
full backup. Write it outside `backups/` so the daily prune doesn't sweep it:
```bash
docker exec ac-database mysqldump -uroot -p"$DOCKER_DB_ROOT_PASSWORD" \
    acore_characters > /tmp/acore_characters-$(date +%F).sql
# Restore that single DB:
docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" \
    acore_characters < /tmp/acore_characters-2026-05-20.sql
```

---

## Rndbot Reset (DESTRUCTIVE)

> **WARNING:** The queries below permanently delete all rndbot accounts and characters and all associated data (items, mail, guilds, groups, etc.). Take a full backup first (admin **Backups** page, or `/opt/stacks/azerothcore/backup.sh`). After running, the server must be restarted and will regenerate the bot pool from scratch — this takes time.
>
> Use this when: you want to change the bot count significantly, the bot pool is corrupted, or you need a clean slate.

```sql
-- Back up first (full archive): /opt/stacks/azerothcore/backup.sh

USE `acore_playerbots`;
DELETE FROM `playerbots_random_bots`;
DELETE FROM `playerbots_account_type`;

USE `acore_characters`;
DELETE FROM `characters` WHERE `account` IN (SELECT `id` FROM `acore_auth`.`account` WHERE `username` LIKE 'RNDBOT%') OR `account` NOT IN (SELECT `id` FROM `acore_auth`.`account`);
DELETE FROM `arena_team_member` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `arena_team` WHERE `arenaTeamId` NOT IN (SELECT `arenaTeamId` FROM `arena_team_member`);
DELETE FROM `character_account_data` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_achievement` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_achievement_progress` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_action` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_aura` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_glyphs` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_homebind` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `item_instance` WHERE `owner_guid` NOT IN (SELECT `guid` FROM `characters`) AND `owner_guid` > 0;
DELETE FROM `character_inventory` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_pet` WHERE `owner` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `pet_aura` WHERE `guid` NOT IN (SELECT `id` FROM `character_pet`);
DELETE FROM `pet_spell` WHERE `guid` NOT IN (SELECT `id` FROM `character_pet`);
DELETE FROM `pet_spell_cooldown` WHERE `guid` NOT IN (SELECT `id` FROM `character_pet`);
DELETE FROM `character_queststatus` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_queststatus_rewarded` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_reputation` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_skills` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_social` WHERE `friend` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_spell` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_spell_cooldown` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_talent` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `corpse` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `groups` WHERE `leaderGuid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `group_member` WHERE `memberGuid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `mail` WHERE `receiver` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `mail_items` WHERE `receiver` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `guild` WHERE `leaderguid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `guild_bank_eventlog` WHERE `guildid` NOT IN (SELECT `guildid` FROM `guild`);
DELETE FROM `guild_member` WHERE `guildid` NOT IN (SELECT `guildid` FROM `guild`) OR `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `guild_rank` WHERE `guildid` NOT IN (SELECT `guildid` FROM `guild`);
DELETE FROM `petition` WHERE `ownerguid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `petition_sign` WHERE `ownerguid` NOT IN (SELECT `guid` FROM `characters`) OR `playerguid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_arena_stats` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);
DELETE FROM `character_entry_point` WHERE `guid` NOT IN (SELECT `guid` FROM `characters`);

USE `acore_auth`;
DELETE FROM `account` WHERE `username` LIKE 'RNDBOT%';
DELETE FROM `realmcharacters` WHERE `acctid` NOT IN (SELECT `id` FROM `account`);
```

After running: restart the worldserver. The bot pool will regenerate based on your current config.

> Source: [noisiver/Revision](https://github.com/noisiver/codebase/blob/master/SQL/AzerothCore/delete_playerbots.sql)
