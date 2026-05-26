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

---

## Common Issues

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
