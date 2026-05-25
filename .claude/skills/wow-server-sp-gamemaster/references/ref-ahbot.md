# AH Bot Reference (mod-ah-bot-plus)

## Overview

The Auction House Bot automatically populates both faction auction houses with items, creating an economy even without real players. It uses AH bot characters to buy and sell.

## Setup (Done During Install)

**Pause 3** of the install creates the AH bot characters:
1. Log into WoW client as the `ahbot` account
2. Create characters on both Alliance and Horde
3. Log out
4. Re-run the installer — it discovers GUIDs and writes them to `mod_ahbot.conf`

The GUIDs are stored in:
```
/opt/stacks/azerothcore/configs/modules/mod_ahbot.conf
```
Key: `AuctionHouseBot.GUIDs = <guid1>,<guid2>`

**This is the ONLY `.conf` file the installer edits post-install.**

> `AuctionHouseBot.GUIDs` is **blocked** in the admin web UI — it must be managed via the installer, not the Settings page.

## Key Configuration

AH bot is controlled via AC_* env vars in `docker-compose.override.yml`. Key env vars:

| Env Var | Config Key | Description |
|---------|-----------|-------------|
| `AC_AUCTION_HOUSE_BOT_ENABLE_SELLER` | `AuctionHouseBot.EnableSeller` | Enable seller bot (1=on) |
| `AC_AUCTION_HOUSE_BOT_BUYER_ENABLED` | `AuctionHouseBot.Buyer.Enabled` | Enable buyer bot (1=on) |
| `AC_AUCTION_HOUSE_BOT_ITEMS_PER_CYCLE_BOOST` | `AuctionHouseBot.ItemsPerCycle.Boost` | Items listed per cycle when boosting |
| `AC_AUCTION_HOUSE_BOT_ITEMS_PER_CYCLE_NORMAL` | `AuctionHouseBot.ItemsPerCycle.Normal` | Items listed per cycle normally |

> For the full list of AH bot config options, see `docs/configs/mod_ahbot.conf.dist`.

## Verifying AH Bot is Working

1. Log into WoW client and check the Auction House — items should be listed by bot characters
2. Check that `Errors.log` is 0 bytes (no AH bot errors)
3. The bot characters should appear in the AH as sellers

## Re-creating AH Bot Characters (if lost)

If AH bot characters need to be recreated:
1. Delete old characters (if any) from the `ahbot` account
2. Create new characters on both factions in WoW client
3. Resume installer from phase `6.1.4`:
   ```bash
   ./scripts/install-azerothcore.sh --resume-from=6.1.4
   ```
   This re-discovers GUIDs and rewrites `mod_ahbot.conf`

## Checking GUIDs

```bash
# See current GUIDs in the conf file:
grep "GUIDs" /opt/stacks/azerothcore/configs/modules/mod_ahbot.conf

# Check characters exist in DB:
docker exec -i ac-database mysql -uroot -p"$DOCKER_DB_ROOT_PASSWORD" \
    -e "SELECT guid, name, account FROM acore_characters.characters \
        WHERE account = (SELECT id FROM acore_auth.account WHERE username='ahbot')"
```

## AH Bot Account Creation (Console)

If you need to recreate the `ahbot` account (from worldserver console):
```
account create ahbot <password>
# Do NOT give this account GM level — it's a regular player account
```
