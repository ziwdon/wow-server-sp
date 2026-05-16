# AzerothCore + Playerbots + AH Bot Plus — Docker Installer

Single-file installer for `AzerothCore + mod-playerbots + mod-ah-bot-plus` on Docker, intended for a private home-server setup.

Target setup:

- Ubuntu 22.04 LTS CLI as the primary target
- Ubuntu 24.04 is detected and allowed only after confirmation
- Stack path: `/opt/stacks/azerothcore/`
- Private play with a small number of human players and a few hundred playerbots
- WoW clients connect through Tailscale only; no public IP, no router port forwarding, and no direct-LAN client path in this revision

The repository includes three scripts:

```bash
install-azerothcore.sh
verify-azerothcore.sh
uninstall-azerothcore.sh
```

## What the installer changes

Most AzerothCore files, database bind mounts, configs, backups, and logs are placed under:

```bash
/opt/stacks/azerothcore/
```

The installer also intentionally changes or creates some system/user-level items outside that directory:

- Installs required apt packages if missing
- Installs/configures Docker if missing
- Adds the invoking user to the `docker` group if needed
- Installs/authenticates Tailscale if needed
- Creates installer state at `~/.azerothcore-install-state`
- Temporarily creates prompt config at `~/.azerothcore-install-config`
- Creates temporary logs under `/tmp` before relocating logs into the stack directory
- Adds a backup cron entry
- Optionally configures UFW, if selected during the prompt
- Optionally creates/enables `/etc/systemd/system/azerothcore.service`, if selected during the prompt

It does **not** require public networking, router port forwarding, or public exposure of WoW ports.

## Prerequisites

- Ubuntu 22.04 LTS CLI recommended
- Around 50 GB free under `/opt`
- Internet access for apt, GitHub, Docker Hub, and Tailscale
- A Tailscale account you can authenticate against in a browser
- `sudo` rights for the invoking user
- Run the scripts as your normal user, not with `sudo`

## Download/copy scripts

Clone the repo and copy the `scripts/` folder to any normal user-owned location, for example:

```bash
git clone https://github.com/ziwdon/wow-server-sp ~/azerothcore-install
cd ~/azerothcore-install/scripts
```

The installer uses the fixed stack path `/opt/stacks/azerothcore/`, so it does not need to be executed from inside the stack directory.

## Set permissions

```bash
chmod +x scripts/install-azerothcore.sh scripts/verify-azerothcore.sh scripts/uninstall-azerothcore.sh
```

Alternatively, you can run them with `bash scripts/script-name.sh`, but executable permissions are recommended.

## Do not run with sudo

Run the scripts as your normal user:

```bash
./scripts/install-azerothcore.sh
./scripts/verify-azerothcore.sh
./scripts/uninstall-azerothcore.sh
```

Do **not** run them like this:

```bash
sudo ./install-azerothcore.sh
sudo ./verify-azerothcore.sh
sudo ./uninstall-azerothcore.sh
```

The scripts call `sudo` internally only where needed. Running the whole script as root can cause incorrect ownership, wrong `$HOME`, wrong crontab cleanup, root-owned installer state, and incorrect Docker UID/GID settings.

## Run installer

```bash
./install-azerothcore.sh
```

The script prompts for configuration values up front, including:

- DB root password, or Enter to generate one automatically
- GM account username/password
- AHBOT account password
- random playerbot count
- MySQL buffer pool size
- map update threads
- AH bot character count
- whether to configure UFW
- whether to enable systemd auto-start

Prompt answers are persisted to `~/.azerothcore-install-config` with mode `600` while the install is resumable. This file is shredded on successful completion.

Manual passwords are intentionally restricted to shell-safe characters:

```text
letters, numbers, . _ @ % + = , : -
```

This avoids problems when the installer resumes and sources its saved config.

## Manual pauses

The installer has three manual pauses.

### 1. Tailscale authentication

During Phase 0.4, the script runs:

```bash
sudo tailscale up
```

Open the printed URL in a browser, authenticate, and return to the terminal. The script polls for a Tailscale IPv4 address and continues automatically.

### 2. Account creation

After the first server start and DB initialization, the script asks you to attach to the worldserver console from a second terminal:

```bash
docker attach ac-worldserver
```

Then enter the account commands shown by the installer. Detach with:

```text
Ctrl+P, then Ctrl+Q
```

Do **not** use `Ctrl+C`, because that can stop the worldserver container.

For safety, the installer avoids writing the real account passwords into the install log. The terminal still shows the real commands during the manual step.

### 3. AH bot character creation

Log into the WoW 3.3.5a client using the `AHBOT` account and create the configured number of AH bot characters. Log out after creation. The installer then detects the created character GUIDs and writes them into `mod_ahbot.conf`.

## Resume after failure or interruption

```bash
./install-azerothcore.sh                       # auto-resumes
./install-azerothcore.sh --resume-from=2.5     # force re-run from phase 2.5
./install-azerothcore.sh --force-from=2.5      # alias of the above
```

Use this to continue after a failure, reboot, SSH disconnect, or logout/login after Docker group membership changes.

List available phases:

```bash
./install-azerothcore.sh --help
```

## Wipe and start over using the installer

```bash
./install-azerothcore.sh --force-fresh
```

This removes:

- the installer state file
- the stack directory under `/opt/stacks/azerothcore/`
- the temporary persisted config file

It asks for explicit `WIPE` confirmation first.

Use `--force-fresh` when you want to restart the installer flow from scratch, but do not need to remove cron/systemd artifacts separately.

## Uninstall/reset script

Use the uninstall script when you want to remove this stack's local artifacts after an install attempt or completed install.

Dry run first:

```bash
./uninstall-azerothcore.sh --dry-run
```

Then run:

```bash
./uninstall-azerothcore.sh
```

Or skip confirmation:

```bash
./uninstall-azerothcore.sh --yes
```

The uninstall script removes or cleans up:

- `/opt/stacks/azerothcore/`
- `~/.azerothcore-install-state`
- `~/.azerothcore-install-config`
- matching backup cron lines pointing to `/opt/stacks/azerothcore/backup.sh`
- optional `/etc/systemd/system/azerothcore.service`, if present
- known AzerothCore containers such as `ac-database`, `ac-authserver`, `ac-worldserver`, `ac-db-import`, and `ac-client-data-init`
- known temporary installer files under `/tmp`

It intentionally does **not** uninstall or remove:

- Docker
- Tailscale
- UFW
- apt packages installed by the installer
- Docker images/cache
- non-AzerothCore containers
- non-AzerothCore cron jobs
- non-AzerothCore firewall rules
- your user's Docker group membership

### Docker compose cleanup scope

The uninstall script uses project-scoped Docker Compose cleanup for the AzerothCore project and then removes only known AzerothCore containers by name.

`docker compose down` acts on the current Compose project, not every Docker container on the machine. The potentially broader option is `--remove-orphans`, because it can remove containers that have the same Compose project label but are no longer listed in the current Compose file.

To avoid accidental removal of unrelated containers, the uninstall script should avoid `--remove-orphans` and rely on:

```bash
docker compose -p azerothcore down
```

plus explicit cleanup of known AzerothCore container names. This should not affect unrelated Docker containers unless they were created using the same Compose project name and conflicting container names.

## Adopt an existing install

If the stack directory already exists but the state file does not, for example after a manual install or lost state file:

```bash
./install-azerothcore.sh --adopt
```

Adopt mode verifies the existing install before marking phases complete. If checks fail, it aborts without blindly marking state as complete.

## Verify installation

```bash
./verify-azerothcore.sh
```

The verification script checks, among other things:

- long-running containers are running
- init containers exited successfully
- required databases exist
- MySQL tuning is active
- realmlist matches the Tailscale IPv4 address
- locally built images use the expected tag
- AH bot config has non-zero GUIDs
- playerbots config exists and is enabled
- backup script exists
- backup cron entry exists
- systemd unit is enabled, if you opted into systemd

It exits `0` on pass and `1` if any required check fails.

## Logs, temporary files, and sensitive files

Install and build logs are expected during the run. They are useful for troubleshooting and can be kept until the server has been verified.

Main log locations:

```bash
/opt/stacks/azerothcore/logs/install-<timestamp>.log
/opt/stacks/azerothcore/logs/backup.log
/tmp/ac-build.log
docker logs ac-worldserver
```

Behavior:

- The main installer log starts under `/tmp` and is later moved into `/opt/stacks/azerothcore/logs/`.
- `/tmp/ac-build.log` is created during the Docker build phase and may remain after installation until manually deleted or cleared by the system after reboot.
- Build logs can contain many compiler warnings. That is normal as long as the build continues and the installer does not stop with a fatal error.
- Log files of a few MB, or even tens of MB during a C++ Docker build, are normal and not usually a disk-space concern.

To check log sizes:

```bash
ls -lh /tmp/ac-build.log 2>/dev/null || true
ls -lh /opt/stacks/azerothcore/logs/
df -h /
```

After a successful install and verification, you may delete temporary or old installer logs:

```bash
rm -f /tmp/ac-build.log
rm -f /tmp/azerothcore-install-*.log
```

Optionally, after you have confirmed the server starts, login works, backups work, and bots/AH setup is complete, you can remove old stack installer logs as well:

```bash
rm -f /opt/stacks/azerothcore/logs/install-*.log
```

Keep `/opt/stacks/azerothcore/logs/backup.log` if you want backup history. Deleting logs does not remove the game server, database, configs, Docker images, or backups.

Do not publicly share generated runtime files without reviewing them first. In particular, do not publish:

```bash
/opt/stacks/azerothcore/.env
/opt/stacks/azerothcore/backups/
/opt/stacks/azerothcore/logs/
~/.azerothcore-install-config
/tmp/azerothcore-install-*.log
/tmp/ac-build.log
/tmp/ac-compose-effective.*.yml
```

The current installer redacts or avoids logging the most obvious account-password output, but generated runtime files may still contain private local configuration.

## Post-install tuning

### AH bot

Edit:

```bash
/opt/stacks/azerothcore/configs/modules/mod_ahbot.conf
```

Then run `.ahbot reload` in the WoW client as your GM character. A worldserver restart is usually not required for simple AH bot setting changes.

### Playerbots

Edit one of these, depending on the module revision:

```bash
/opt/stacks/azerothcore/configs/modules/playerbots.conf
/opt/stacks/azerothcore/configs/modules/mod_playerbots.conf
```

Then restart the worldserver:

```bash
cd /opt/stacks/azerothcore
docker compose restart ac-worldserver
```

### MySQL tuning

Edit:

```bash
/opt/stacks/azerothcore/configs/mysql/custom.cnf
```

Then restart the database container:

```bash
cd /opt/stacks/azerothcore
docker compose restart ac-database
```

`innodb_buffer_pool_size` requires a database restart to take effect.
