# Troubleshooting Reference

## Log Locations

All runtime logs are written to `/opt/stacks/azerothcore/logs/` on the host (bind-mounted from `./logs` inside `ac-worldserver` at `/azerothcore/env/dist/logs/`).

| File | Purpose | Notes |
|------|---------|-------|
| `Errors.log` | Runtime errors only | Mode `w` (truncated on each boot). **0 bytes = clean.** Authoritative signal. |
| `Server.log` | Boot / init output | Mode `w` (truncated on each boot). Quiet after `WORLD: World Initialized`. |
| `Playerbots.log` | Bot activity | Mode `w` (truncated on each boot). Chatty; see benign-noise section below. |
| `backup.log` | Nightly cron backup output | Appended by cron (`>>`); grows over time. |
| `install-<unix-ts>.log` | Full install transcript | Written to `/tmp/` first; relocated here once the dir exists. |

Live stdout (not written to any file on disk):
- `docker logs ac-worldserver` — worldserver stdout; authoritative for the bot stats block
- `docker logs ac-authserver` — authserver connection events
- `docker logs ac-database` — MySQL startup and errors

## Triage Order

1. **Check `Errors.log` size first:**
   ```bash
   ls -la /opt/stacks/azerothcore/logs/Errors.log
   # 0 bytes = no runtime errors. If non-zero, read it:
   tail -100 /opt/stacks/azerothcore/logs/Errors.log
   ```

2. **Check live worldserver output:**
   ```bash
   docker logs --tail 100 ac-worldserver
   docker logs --follow ac-worldserver   # Stream in real time
   ```

3. **Check container status:**
   ```bash
   docker ps
   # All three should be Up: ac-worldserver, ac-authserver, ac-database
   ```

4. **Check Server.log** (for boot issues only):
   ```bash
   tail -100 /opt/stacks/azerothcore/logs/Server.log
   # Note: frozen mtime after "World Initialized" is NORMAL
   ```

5. **Check Playerbots.log** (for bot-related issues):
   ```bash
   tail -200 /opt/stacks/azerothcore/logs/Playerbots.log
   ```

6. **Check backup.log** (for backup failures):
   ```bash
   cat /opt/stacks/azerothcore/logs/backup.log
   ```

7. **Check install log** (for install or phase failures):
   ```bash
   # After relocation to the stack logs dir:
   ls -t /opt/stacks/azerothcore/logs/install-*.log 2>/dev/null | head -1 | xargs tail -100
   # Or still in /tmp/ (install running or never relocated):
   ls -t /tmp/azerothcore-install-*.log 2>/dev/null | head -1 | xargs tail -100
   ```

---

## Known-Benign Log Noise (Do NOT Chase These)

### Install log (Phase 3 build)
- Hundreds of clang `-Wsign-compare`, `-Wdeprecated-copy`, `-Wimplicit-const-int-float-conversion` warnings from `mod-playerbots` sources — **normal upstream build noise, build still succeeds**

### Server.log
- `mysql: [Warning] Using a password on the command line interface can be insecure.` — expected; scripts pass passwords non-interactively
- `Can't set process priority class, error: Permission denied` — worldserver lacks `CAP_SYS_NICE` in container, cosmetic
- `MoveSplineInitArgs::Validate: expression 'velocity > 0.01f' failed for GUID…` — upstream world-DB data quirk, cosmetic
- `>> The file 'YYYY_MM_DD_NN.sql' was applied to the database, but is missing in your update directory now!` — high volume (~2500+ lines per boot); DB still concludes "up-to-date", informational only
- **Frozen `Server.log` mtime after `WORLD: World Initialized`** — **NORMAL**, not a stall; runtime traffic goes to `Playerbots.log` and `docker logs ac-worldserver`

### Playerbots.log
- `<BotName> A:<action> - FAILED` (e.g., `A:follow - FAILED`) — action-retry traces, **expected high volume**
- `Can cast spell failed. No spellid. - spellid: 0` — inapplicability trace, normal
- `Random teleporting bot <Name>…` — normal periodic relocation, not an error

### docker logs ac-worldserver
- `Random Bots Stats: 0 online` with all zeros — **NORMAL when no real player is logged in** (due to `AC_AI_PLAYERBOT_DISABLED_WITHOUT_REAL_PLAYER=1`)

### Errors.log
- `Table \`graveyard_zone\` incomplete: Zone <id> Team <0|1> does not have a linked graveyard` — an upstream **data gap**, functionally benign (the server falls back to the default graveyard). Triggered by playerbot deaths in zones AzerothCore ships no graveyard for, so it surfaces only after hours of uptime. Does break the "0 bytes = clean" signal. See **"`graveyard_zone` incomplete errors"** under Common Issues for the root cause and the exact fix SQL.

---

## Common Issues

### `graveyard_zone` incomplete errors in Errors.log

```
Table `graveyard_zone` incomplete: Zone 2037 Team 0 does not have a linked graveyard.
Table `graveyard_zone` incomplete: Zone 3455 Team 1 does not have a linked graveyard.
```

**Root cause.** `Graveyard::GetClosestGraveyard()` (`src/server/game/Misc/GameGraveyard.cpp:168`) logs this on the `sql.sql` channel when an entity needs a *player* graveyard in a zone with no row in the `graveyard_zone` table for its team (and the map isn't a battleground/arena). It then falls back to `GetDefaultGraveyard()` — Westfall (Alliance) / Crossroads (Horde) — so **gameplay is unaffected**; this is log noise, but it makes `Errors.log` non-zero.

AzerothCore ships **no graveyard link** for a few zones a real player essentially never dies in (confirmed: absent from `data/sql/base/db_world/graveyard_zone.sql` and all update files — a genuine upstream gap, not local corruption):
- **Zone 2037 = Quel'thalas** (map 0, a vanilla leftover sliver in the far-north Eastern Kingdoms, bordering the Eastern Plaguelands)
- **Zone 3455 = The North Sea** (map 530, the open ocean around the blood-elf/Quel'Danas isle)

It surfaces **after hours of uptime, not at boot**, because it needs a death event there — playerbots roam, get RandomBot-teleported, swim, and drown, so one eventually dies in these zones. (Identify any other reported zone ID by parsing `AreaTable.dbc`, field 0 = ID, field 11 = enUS name.)

**Fix** — add a neutral (`Faction=0` = serves both factions) link per zone, pointing at the nearest existing graveyard on the **same map**. The IDs below are graveyards AzerothCore *already* classifies neutral (`Faction=0`) for their own adjacent zones, so they're proven safe for Alliance and Horde:

```sql
INSERT INTO acore_world.graveyard_zone (ID, GhostZone, Faction, Comment) VALUES
  (1448, 2037, 0, 'Quel''thalas -> EPL Northdale (custom: fill upstream graveyard_zone gap)'),
  (922,  3455, 0, 'The North Sea -> Eversong Fairbreeze GY (custom: fill upstream graveyard_zone gap)');
```
- `1448` = Eastern Plaguelands, Northdale (map 0) — neutral contested PvE, no faction guards
- `922` = Eversong Woods, Fairbreeze GY (map 530) — nearest blood-elf landmass GY

Apply live without a restart (the in-memory store is only refreshed on reload/boot):
```bash
docker attach ac-worldserver
reload graveyard_zone            # then detach: Ctrl-P Ctrl-Q  (NEVER Ctrl-C)
# console should print: >> Loaded <N> Graveyard-Zone Links
```

**Verify / revert:**
```bash
source /opt/stacks/azerothcore/.env
docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" \
  -e "SELECT * FROM acore_world.graveyard_zone WHERE GhostZone IN (2037,3455);"
# Revert:
docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" \
  -e "DELETE FROM acore_world.graveyard_zone WHERE (ID=1448 AND GhostZone=2037) OR (ID=922 AND GhostZone=3455);"
```

**Durability caveat.** A direct `INSERT` persists across restarts (the world DB is not wiped on boot — DBUpdater only applies *pending* update files), but a **full world re-import** (fresh install / wipe) loses it. To make it permanent, add the idempotent SQL to the install pipeline. Pre-existing `Errors.log` lines clear on the next worldserver restart (the `Server`/`Errors` appenders open in mode `w`); the fix stops *new* ones immediately after `reload graveyard_zone`.

### Playerbots pile up on gryphons at continent borders (stuck taxi flight)

**Symptom.** Groups of bots sit motionless on flight-path gryphons, stacked on top of each other, at the edge of a continent — most visibly the **Gates of Ironforge** (map 0, ≈ `-5034 -819 520`). They never dismount or move; the count grows the longer the server runs. Other clusters form at any cross-continent route's origin-map border (e.g. the Ghostlands ⇄ Outland boundary).

**Root cause.** The mod-playerbots "New RPG" engine has idle bots randomly pick `RPG_TRAVEL_FLIGHT`, fly to a flight master and call `ActivateTaxiPathTo`. The destination is chosen by level-bracket / capital city with **no same-continent constraint** (`TravelMgr::GetOptimalFlightDestinations` → `FindTaxiPath`). The WotLK 3.3.5 data places the Isle of Quel'Danas / Sunwell "Shattered Sun" and Zul'Aman/Ghostlands taxi nodes on **map 530** (the TBC "Expansion01" map) and ships real direct flights from capital cities to them (e.g. `TaxiPath` 807: node 6 *Ironforge, map 0* → node 213 *Shattered Sun, map 530*). Bots are flagged taxi-cheaters (`PlayerbotAI.cpp:137-138`), so they bypass the "node known" and gold gates and get routed onto these cross-map flights.

A taxi flight that crosses a map boundary only advances when the **game client** sends two packets: `CMSG_MOVE_SPLINE_DONE` (→ `HandleMoveSplineDoneOpcode`, `TaxiHandler.cpp`, which teleports the player to the next map) and then `MSG_MOVE_WORLDPORT_ACK` (→ `HandleMoveWorldportAck`, `MovementHandler.cpp`, which re-initialises the flight on the new map). A bot has no client, and mod-playerbots never synthesises `CMSG_MOVE_SPLINE_DONE`, so the cross-map teleport is never initiated. The server-side spline finishes at the last node on the origin map and the bot stays `UNIT_STATE_IN_FLIGHT` forever, frozen at the border. **Same-continent flights are unaffected** (they finalise server-side). This is **not** caused by mod-individual-progression, and it is **not player-facing** — real clients send the packets, so player flights work normally.

**Restart behaviour (a partial cleaner, not a fix).** On login a saved `taxi_path` is reloaded and `Player::ContinueTaxiFlight()` (`Player.cpp:10397`) either *resumes* or *clears* it: it searches the current leg's nodes for a segment on the bot's **current map**. Bots frozen *at* the continent boundary were saved at the last node on their origin map, so no same-map segment remains ahead — `startNode == 0` and the taxi is **cleared** (`:10453-10456`), unsticking them on relogin. Bots saved *mid-route* (a same-map segment still ahead) **resume** and keep flying. So a restart clears the boundary-frozen pile-ups (e.g. the Ironforge gates cluster) but not every taxi-carrying bot — and, crucially, **without disabling RPG flights (Solution A) the cleared bots just pick fresh cross-continent flights and re-accumulate.** A restart alone is therefore not a durable fix.

**Detection.**
```bash
source /opt/stacks/azerothcore/.env
docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" -e "
SELECT map, COUNT(*) n FROM acore_characters.characters
WHERE taxi_path<>'' AND online=1 GROUP BY map ORDER BY n DESC;"
```
A persistent population of online characters with a non-empty `taxi_path` (often clustered at identical coordinates) is the signature. The `taxi_path` format is `<flightMasterFactionId> <srcNode> <node…>`; map a node id to its continent via the `ContinentID` field of `TaxiNodes.dbc`.

#### Solution A — config mitigation (no rebuild, instant, reversible)

Stop bots from ever auto-selecting a flight by zeroing the RPG flight weight:

- Key: `AiPlayerbot.RpgStatusProbWeight.TravelFlight = 0` (default `15`)
- Env var: `AC_AI_PLAYERBOT_RPG_STATUS_PROB_WEIGHT_TRAVEL_FLIGHT=0`
- Apply via the admin app (Settings → set the key → Apply), or add the env var to `docker-compose.override.yml` / `docker-compose.admin.yml` and restart the worldserver.

These eight RPG-status weights are **relative**, not absolute percentages (they sum to 150, not 100). Setting `TravelFlight` to `0` simply drops it from the weighted lottery — the other statuses keep their relative proportions automatically, so there is **no need to redistribute the 15** to the other weights.

Trade-off: disables **all** bot RPG flights, including the same-continent ones that work fine, so bots no longer ride gryphons at all. They still relocate across continents via the independent `RandomPlayerbotMgr` teleport system (direct teleport, no flight), so zone population and redistribution are unaffected — the loss is purely cosmetic.

#### Solution B — code fix (recommended; requires a worldserver rebuild)

Restrict bot flight selection to same-continent routes. In `modules/mod-playerbots/src/Mgr/Travel/TravelMgr.cpp`, `GetOptimalFlightDestinations`, reject any candidate route whose nodes are not all on the bot's current map (compare each node's `TaxiNodesEntry->map_id` to the start node's map before returning the path). This keeps working same-continent flights and eliminates the stalls. It is **bot-only** — the core taxi/movement code and player flights are untouched.

Deploy with the isolated redeploy script (**not** the installer — `install-azerothcore.sh --resume-from=3` would also run Phase 4 DB-init, account-creation pauses, etc.):
```bash
./scripts/redeploy-azerothcore.sh
```
No DB impact — characters, progression and items are untouched; only the worldserver image is rebuilt and the container recreated. Because the module source lives in the gitignored stack dir, carry the change as a repo patch applied right after the Phase-1 module clone so it survives a fresh install.

#### Cleanup — unstick the bots already frozen

A restart clears the boundary-frozen bots but *resumes* any saved mid-route (see **Restart behaviour** above), so on its own it is not a complete cleanup. **In practice, applying Solution A via the admin app already restarts the worldserver** — which clears the bulk on relogin — and any residual in-progress flights then drain within the 1–5 h `RandomBotTeleportInterval` (each bot's periodic `RandomTeleportForLevel` teleports it away and clears the taxi). So a separate cleanup is usually unnecessary. If you do want a deterministic, instant full clear, wipe `taxi_path` while the worldserver is **stopped** — for an online bot the authoritative flight state is in memory, so a live `UPDATE` is ignored and overwritten on the next save:
```bash
cd /opt/stacks/azerothcore
docker compose stop -t 120 ac-worldserver
source .env
docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" -e "
UPDATE acore_characters.characters SET taxi_path='' WHERE taxi_path<>'';"
docker compose up -d ac-worldserver
```
Pair this with Solution A or B — otherwise bots will re-stick on the next round of RPG flights.

### Post-Unexpected-Shutdown Verification (power loss, forced reboot, OOM kill)

Run these in order — stop at the first failure and investigate before continuing:

```bash
# 1. Confirm all three containers are up
docker ps --format "table {{.Names}}\t{{.Status}}" | grep ac-
# Expected: ac-worldserver, ac-authserver, ac-database — all Up

# 2. Check MySQL recovered cleanly
docker logs ac-database 2>&1 | grep -E "InnoDB|ERROR|crash|recover" | tail -30
# InnoDB self-recovers; look for "ready for connections" at the end, no [ERROR] lines

# 3. Check Errors.log — must be 0 bytes
ls -la /opt/stacks/azerothcore/logs/Errors.log

# 4. Confirm worldserver is healthy
# "World Initialized" may not be in the tail if server has been up for minutes —
# the bot stats block in stdout is equally authoritative
docker logs --tail 200 ac-worldserver 2>&1 | tail -30

# 5. MySQL table check (only if steps above show errors)
source /opt/stacks/azerothcore/.env
docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" \
    -e "CHECK TABLE acore_characters.characters, acore_characters.character_inventory, acore_auth.account;"
# All rows must return status: OK
```

InnoDB is crash-safe — actual corruption is rare. The main risk is a few seconds of in-flight character saves lost (data loss, not corruption). If containers are not up, start them: `cd /opt/stacks/azerothcore && docker compose up -d`

### Containers not running
```bash
docker ps | grep ac-
# If missing, start them:
cd /opt/stacks/azerothcore
docker compose up -d
```

### Worldserver crashed / restarting loop
```bash
docker logs ac-worldserver | tail -100
# Look for panics, assertions, or DB errors
# Also check Errors.log
```

### Database errors on boot
```
[ERROR]: Table 'acore_world.table' doesn't exist
```
Database is not up to date. The Docker image auto-updates on start if configured correctly. If not:
```bash
# Connect to database:
docker exec -it ac-database mysql -uroot -p<DOCKER_DB_ROOT_PASSWORD>
# Then check DB update status
```

### "Random Bots Stats: 0 online" when player is logged in
- Check `AC_AI_PLAYERBOT_ENABLED=1` in override.yml
- Check `AC_AI_PLAYERBOT_DISABLED_WITHOUT_REAL_PLAYER=1` (bots should activate when you log in)
- Give it a few minutes — bots ramp up gradually
- Run `playerbot rndbot stats` in worldserver console to check state

### Bot pool integrity check
```bash
docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" \
    -e "SELECT COUNT(*) FROM acore_characters.characters c \
        JOIN acore_auth.account a ON a.id=c.account \
        WHERE a.username LIKE 'RNDBOT%'"
# Expected count = configured_accounts × chars_per_account
# Check your configured bot count:
grep -E "AC_AI_PLAYERBOT_(MIN|MAX)_RANDOM_BOTS" /opt/stacks/azerothcore/docker-compose.override.yml
```

### Cannot connect with WoW client
```bash
# Check Tailscale is running:
tailscale status
# Check realmlist.wtf in WoW client Data/enUS/ folder:
# Should be: set realmlist <tailscale-ip>
# Check authserver is up:
docker ps | grep ac-authserver
```

### GM commands not working
- Verify account has GM level: `account set gmlevel <account> 3 -1` (from console)
- In-game commands need leading dot: `.gm on`
- Console commands don't need leading dot

### Server is laggy / high diff time
```
# In-game: .server info
# Diff time should be < 70-80ms. If higher:
```
1. Check bot count — if `BotActiveAlone = 100` and many bots are active, set to `10`
2. Increase `MapUpdate.Threads` (set to 4-6 in override.yml, restart worldserver)
3. Check for memory pressure: `free -h` on host
4. Restart worldserver (memory footprint grows over time with bots)

### Admin web app not accessible
```bash
# Check admin container:
docker ps | grep azerothcore-admin
# Check admin logs:
docker logs azerothcore-admin
# Verify tailscale IP matches .env:
cat /opt/stacks/azerothcore-admin/.env | grep TAILSCALE_IP
tailscale ip -4
```

### Admin Apply broke the server
1. Go to admin web app → Settings → Rollback
2. Or manually restore the snapshot:
   ```bash
   ls /opt/stacks/azerothcore-admin/snapshots/
   cp /opt/stacks/azerothcore-admin/snapshots/admin.yml.bak.<ts> \
      /opt/stacks/azerothcore/docker-compose.admin.yml
   docker restart ac-worldserver
   ```

### Install failed at a phase
```bash
# Check log:
cat /tmp/azerothcore-install-<ts>.log | tail -50
# Or after relocation:
cat /opt/stacks/azerothcore/logs/install-<ts>.log | tail -50
# Resume from last good phase:
./scripts/install-azerothcore.sh --resume-from=<phase>
```

---

## AzerothCore Error Codes

| Code | Symptom | Fix |
|------|---------|-----|
| ACE00001 | `Table 'acore_world.table' doesn't exist` | Run DB updates |
| ACE00002 | `Cannot connect to world database` | Check DB running, credentials |
| ACE00003 | `Loaded 0 acore strings. DB table acore_string is empty.` | DB not imported at all |
| ACE00004 | `Unknown column 'level'` | Binaries/DB version mismatch |
| ACE00040 | `dbc exists, and has N field(s) (expected M)` | Wrong client version DBC files |
| ACE00043 | `AzerothCore does not support MySQL versions below 8.0` | Upgrade MySQL |
| ACE00045 | `Map file is from incompatible map version` | Recompile tools and re-extract maps |

> Full error code list: `docs/wikis/azerothcore-wiki/docs/common-errors.md`

---

## Useful Diagnostic Commands

```bash
# Server uptime and version:
docker exec ac-worldserver cat /proc/uptime

# DB sizes:
docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" \
    -e "SELECT table_schema, ROUND(SUM(data_length+index_length)/1024/1024,1) AS 'MB' \
        FROM information_schema.tables GROUP BY table_schema"

# Check worldserver environment (verify AC_* are loaded):
docker exec ac-worldserver env | grep AC_

# Number of online players:
docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" \
    -e "SELECT COUNT(*) AS online FROM acore_characters.characters WHERE online=1"

# Recent backup list:
ls -la /opt/stacks/azerothcore/backups/
```

---

## Shellcheck Warnings to Ignore

When running `shellcheck scripts/*.sh`:
- **SC2016 on `escape_regex_metachars`** (~line 775): Single quotes in `sed 's/[.[\*^$()+?{}|]/\\&/g'` are intentional — `\&` is sed's back-reference; double quotes would break it.
- **SC2001 on multi-line `sed 's/^/    - /'`**: Correct tool for adding per-line prefix to multi-line strings.
- **SC2012 on `ls modules/mod-…/ | head -10`** (~lines 2414-2416): Informational stdout only; safe in this context.

---

## GitHub Issue Search (Fallback)

Use this **only** after reading the relevant reference file and local wikis haven't resolved the issue.

### Step 1: Strip local noise from the error

Remove everything installation-specific before composing a search query:
- File paths (`/opt/stacks/…`, `/home/…`)
- Port numbers, IP addresses, Tailscale addresses
- Line numbers (`line 42`, `:3477`)
- UUIDs and character GUIDs
- Timestamps and dates

Keep: exception class, function/module name, error code, error message text, table or column names.

**Example:**
```
Raw:    [ERROR] /opt/stacks/azerothcore/data/dbc/Spell.dbc: Cannot open file (Error 2)
Search: Cannot open file dbc Spell.dbc Error 2
```

### Step 2: Choose the repo

| Error context | Repo |
|--------------|------|
| Core server, authserver, DB, maps, DBC | `azerothcore/azerothcore-wotlk` |
| Playerbot behaviour, AI, commands | `mod-playerbots/mod-playerbots` |
| Auction House Bot | `azerothcore/mod-ah-bot` |
| Individual Progression | `ZhengPeiRu21/mod-individual-progression` |
| Unclear | Start with `azerothcore/azerothcore-wotlk`, then broaden |

### Step 3: Search open AND closed issues

```bash
# Preferred — gh CLI (searches both open and closed in one shot):
gh issue list --repo azerothcore/azerothcore-wotlk \
    --search "your search terms here" --state all --limit 10

# Fallback — curl (if gh is not authenticated or not installed):
curl -s "https://api.github.com/search/issues?q=your+search+terms+repo:azerothcore/azerothcore-wotlk&per_page=10" \
    | python3 -c "
import sys, json
for i in json.load(sys.stdin).get('items', []):
    print(f\"#{i['number']} [{i['state']}] {i['title']}\")
    print(f\"  {i['html_url']}\")
    print()
"
```

Substitute `azerothcore/azerothcore-wotlk` with the appropriate repo from Step 2.

### Step 4: Summarise findings

For each relevant result report:
- Issue title, number, state (open/closed), URL
- Whether a fix is confirmed: an accepted answer, a linked PR, or a maintainer comment saying "fixed in…"

If no useful results in the first repo, repeat with the next most likely repo.

### Step 5: If a closed issue links a PR or commit, fetch the fix

```bash
# With gh CLI:
gh pr view {number} --repo {owner}/{repo} --json title,state,mergedAt,body

# With curl:
curl -s "https://api.github.com/repos/{owner}/{repo}/pulls/{number}" \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['title'], '—', d['state'])
print(d['html_url'])
print()
print((d['body'] or '(no description)')[:800])
"
```

Summarise: what the PR changed, whether it was merged, and whether the fix applies to this installation's version.
