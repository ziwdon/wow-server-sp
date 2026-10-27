-- Pre-db-import SQL 0001: move the six gameobject spawns the old
-- mod-individual-progression inserted by AUTO_INCREMENT to the fixed guids
-- the pinned mod-individual-progression gives them.
--
-- Why: the old module revision (636ba4c6) inserted these spawns WITHOUT
-- explicit guids in data/sql/world/base/aq_gate_quest.sql and
-- naxx40_gameobjects.sql. On this server they got guids 5714438-5714443 (the
-- upstream base maximum is 5714437):
--   5714438 id 176146 map 1, 5714439 id 176147 map 1, 5714440 id 176148 map 1
--   (AQ gate); 5714441 id 361001 map 0, 5714442 id 181056 map 0,
--   5714443 id 193166 map 0 (Naxx40).
-- The core update data/sql/updates/db_world/2026_08_08_09.sql (Hodir chests)
-- hard-codes guids 5714442-5714445, and AC's DBUpdater applies the core
-- updates (sorted by file name) BEFORE the module files that, in the new
-- module revision (4933d455), delete these spawns and re-insert them at fixed
-- guids. Without this file the candidate db-import fails:
--   ERROR 1062 Duplicate entry '5714442' for key 'gameobject.PRIMARY'
-- Moving each row to the guid the new module uses (aq_gate_quest:
-- 176146/176147/176148 -> 650111/650112/650113; naxx40: 361001/181056/193166
-- on map 0 -> 361001/361002/361003) frees 5714438-5714443 and keeps the spawns
-- whether or not the module re-applies its changed files. Nothing references
-- these guids (gameobject_addon, pools, game events, respawns: none).
-- 193166 also spawns once on map 571 (core data): only its map-0 row moves.
--
-- Idempotent: a row already at its target, or absent, is left alone, so a
-- resumed cutover may apply it again (also after db-import ran). A target
-- held by a different row leaves that spawn unmoved; the guard below then
-- counts it and the upgrade stops (fail closed). LIMIT 1 makes the inner
-- derived table non-mergeable, so MySQL always materializes it (the documented
-- exception for reading the updated table; a merged one would be
-- ER_UPDATE_TABLE_USED). MySQL 8.4.9 accepts it without, too.
--
-- upgrade-azerothcore.sh runs the guard after this file; it must print 0.
-- guard: SELECT COUNT(*) FROM acore_world.gameobject WHERE guid >= 5714438 AND (id, map) IN ((176146, 1), (176147, 1), (176148, 1), (361001, 0), (181056, 0), (193166, 0))

UPDATE acore_world.gameobject SET guid = 650111
 WHERE id = 176146 AND map = 1 AND guid <> 650111
   AND NOT EXISTS (SELECT 1 FROM (SELECT guid FROM acore_world.gameobject WHERE guid = 650111 LIMIT 1) AS t);

UPDATE acore_world.gameobject SET guid = 650112
 WHERE id = 176147 AND map = 1 AND guid <> 650112
   AND NOT EXISTS (SELECT 1 FROM (SELECT guid FROM acore_world.gameobject WHERE guid = 650112 LIMIT 1) AS t);

UPDATE acore_world.gameobject SET guid = 650113
 WHERE id = 176148 AND map = 1 AND guid <> 650113
   AND NOT EXISTS (SELECT 1 FROM (SELECT guid FROM acore_world.gameobject WHERE guid = 650113 LIMIT 1) AS t);

UPDATE acore_world.gameobject SET guid = 361001
 WHERE id = 361001 AND map = 0 AND guid <> 361001
   AND NOT EXISTS (SELECT 1 FROM (SELECT guid FROM acore_world.gameobject WHERE guid = 361001 LIMIT 1) AS t);

UPDATE acore_world.gameobject SET guid = 361002
 WHERE id = 181056 AND map = 0 AND guid <> 361002
   AND NOT EXISTS (SELECT 1 FROM (SELECT guid FROM acore_world.gameobject WHERE guid = 361002 LIMIT 1) AS t);

UPDATE acore_world.gameobject SET guid = 361003
 WHERE id = 193166 AND map = 0 AND guid <> 361003
   AND NOT EXISTS (SELECT 1 FROM (SELECT guid FROM acore_world.gameobject WHERE guid = 361003 LIMIT 1) AS t);
