# WoW Client Setup

## Requirements

- **Client version:** World of Warcraft 3.3.5a (build 12340) — no other version works with AzerothCore
- AzerothCore does not distribute the client; you must obtain your own clean 3.3.5a copy

## Connecting via Tailscale (this repo's networking model)

This server uses Tailscale — there is no public IP and no router port forwarding. Every player must have Tailscale installed and connected to the same Tailscale network before they can connect.

### Step 1 — Install Tailscale on the player's machine
Download from https://tailscale.com/download and authenticate with the same Tailscale account (or be invited to the same tailnet).

### Step 2 — Find the server's Tailscale IP
On the server host:
```bash
tailscale ip -4
# Example output: 100.x.y.z
```
Or check `/opt/stacks/azerothcore-admin/.env` — the `TAILSCALE_IP` entry holds the value the installer recorded.

### Step 3 — Edit realmlist.wtf on the player's machine
Navigate to the WoW client's `Data/enUS/` folder (or `Data/<locale>/` for other locales) and open `realmlist.wtf`. Change the first line to:

```
set realmlist 100.x.y.z
```

Replace `100.x.y.z` with the server's actual Tailscale IP.

> Do not use `localhost` or `127.0.0.1` unless connecting from the same machine as the server — use the Tailscale IP even for local LAN connections, because the server binds to its Tailscale address.

### Step 4 — Launch the game
Use `WoW.exe` (not `Launcher.exe`). If you must use Launcher.exe, also set `patchlist` to the same Tailscale IP in `realmlist.wtf`.

---

## Connecting from the Same Machine as the Server

If the player and the server are on the same physical machine, you can still use the Tailscale IP (recommended, to match what remote players use), or use `127.0.0.1` if the authserver binds to all interfaces.

Check what the authserver binds to:
```bash
docker exec ac-authserver grep "BindIP" /opt/stacks/azerothcore/configs/authserver.conf 2>/dev/null || \
grep "AC_BIND_IP" /opt/stacks/azerothcore/docker-compose.override.yml
```

---

## Account Creation

Players cannot self-register — accounts are created by a GM from the worldserver console:

```bash
docker attach ac-worldserver
# Inside the console:
account create <username> <password>
# Detach: Ctrl-P, Ctrl-Q
```

Passwords are restricted to: `letters, numbers, . _ @ % + = , : -`

---

## Realm Selection

After logging in with account credentials, the client will show the realm list. The realm name is configured during install. If the realm does not appear:

1. Confirm Tailscale is connected on the player's machine (`tailscale status`)
2. Confirm the authserver container is running: `docker ps | grep ac-authserver`
3. Confirm `realmlist.wtf` has the correct Tailscale IP (no trailing spaces)
4. Check the realmlist table in the database:
   ```sql
   SELECT * FROM acore_auth.realmlist;
   -- The `address` column should match the Tailscale IP
   ```

---

## Client Data Notes

- The server uses **enUS DBC files** server-side — this is required for the playerbot spell system. The player's game client can be in any language.
- The `Data/` folder in the WoW client directory contains `patch-*.mpq` files. If using `mod-individual-progression` with optional patches (`patch-V.mpq` or `patch-S.mpq`), players who want the optional changes must install the patch file in their client's `Data/` directory. This is optional and per-player.
- Do not use both `patch-V.mpq` and `patch-S.mpq` simultaneously — pick one.
