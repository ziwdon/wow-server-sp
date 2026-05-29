# Playerbot Raid Strategy Guide

> For the full detailed guide, read `docs/wikis/mod-playerbots-wiki/Playerbot-Raid-Strategy-Guide.md`.
> For raid completion status, read `docs/wikis/mod-playerbots-wiki/Playerbot-Raid-Completion-Status.md`.
> The below summarizes key notes and tips per raid.

## General Principles

- Strategies are **auto-applied on instance entry** (bots whisper confirmation)
- IP nerfs (Individual Progression damage/HP adjustments) make many raids more accessible
- Use `rti <icon>` to set kill priority icons
- Use `co +boost` / `co -boost` to control cooldown usage on specific bosses
- Use `rtsc` system to position specific bots at coordinates
- Use `@tank`, `@dps`, `@heal` prefixes to target bot groups

---

## Vanilla Raids

### Molten Core *(completable with IP nerfs)*

| Boss | Solo with Bots | Notes |
|------|---------------|-------|
| Lucifron | Yes, no manual control | Shadow resist aura auto-applied |
| Magmadar | Yes | Fire resist aura; use fear ward + tremor totems manually |
| Gehennas | Yes | Shadow resist auto-applied |
| Garr | Yes | Fire resist; AoE disabled to prevent simultaneous explosions |
| Baron Geddon | Yes | Living Bomb: bots run away; Inferno: bots run from boss |
| Shazzrah | Yes | Ranged positioned at max range |
| Sulfuron Harbinger | Yes | Use Skull to focus adds |
| Golemagg | Yes | Fire resist; AoE disabled; offtanks pull Core Ragers |
| Majordomo | Yes | Shadow resist; use Skull to focus adds |
| Ragnaros | Yes | Fire resist auto-applied |

Enable strategy: `co +moltencore` (auto on entry)

### Blackwing Lair *(completable with IP nerfs)*

All bots receive Onyxia Scale Cloak buff and auto-disable suppression devices.

| Boss | Solo with Bots | Notes |
|------|---------------|-------|
| Razorgore | Yes | **Player must control Razorgore to destroy eggs in Phase 1** |
| Vaelastrasz | Yes | None |
| Broodlord | Yes | None |
| Firemaw/Ebonroc/Flamegor | Yes | None |
| Chromaggus | Yes, with strats | Bots auto-clear Brood Affliction: Bronze; may need RTSC for LOS positioning |
| Nefarian | Yes, with manual control | Phase 2: RTSC tank by stairs. Use `co +tremor` for shamans. Remove `co -tank assist` on main tank in Phase 3 |

Enable strategy: `co +bwl` (auto on entry)

### Ruins of Ahn'Qiraj (AQ20)

| Boss | Notes |
|------|-------|
| Ossirian | Coded strategy exists; rest completable without strats |

Enable strategy: `co +aq20` (auto on entry)

### Zul'Gurub *(completable; no strategies coded)*

Available starting at Tier 3 in Individual Progression. All bosses defeatable with general tactics — no `co +` command exists. Use skull/cross for kill order and standard healing rotation.

### AQ40

| Boss | Notes |
|------|-------|
| Twin Emperors | May need individual strategies |
| C'thun | Final boss of Tier 5 |

### Naxxramas 40-man *(restored by Individual Progression)*

Enable strategy: `co +naxx` (auto on entry — this is WotLK Naxx; Vanilla Naxx is restored by IP mod)

---

## Burning Crusade Raids

### Karazhan *(IP nerfs recommended: 50% bot damage/healing, 2.4.3 HP levels)*

| Boss | Solo with Bots | Tips |
|------|---------------|------|
| Attumen + Midnight | Yes | Bots stack behind Attumen when he mounts |
| Moroes | Yes | Bots prioritize adds by kill order (Millstipe → Von'indi) |
| Maiden of Virtue | Yes | Tank moves Maiden to healer to break Repentance stun |
| Opera | Yes | Player should pull Midnight; Romulo & Julianne swapped with skull; WotF: Big Bad Wolf tanked front-left |
| The Curator | Yes | Save `co +boost` for Evocation (double damage window); `co -boost` before fight |
| Terestian Illhoof | Yes | Kill order: Demon Chains → Kil'rek → Illhoof |
| Shade of Aran | Yes | **Do NOT let bots move during Flame Wreath**; if Aran dies during Flame Wreath, `stay` bots manually |
| Netherspite | Yes, with care | Tanks block Red beam; DPS (non-rogue/warrior) block Blue beam; Healers/Rogues/Warriors block Green beam; boss must be pulled to center room |
| Chess Event | N/A | Player controlled |
| Prince Malchezaar | Yes | None |

Enable strategy: `co +karazhan` (auto on entry)

### Gruul's Lair

Enable strategy: `co +gruulslair` (auto on entry)

### Magtheridon's Lair

Enable strategy: `co +magtheridon` (auto on entry)

### Serpentshrine Cavern

Enable strategy: `co +ssc` (auto on entry)

### Tempest Keep – The Eye *(completable; strategies auto-apply on entry; no explicit `co +` command)*

Full boss-by-boss strategies are coded and documented in `docs/wikis/mod-playerbots-wiki/Playerbot-Raid-Strategy-Guide.md`. For TK-specific notes:
- **Al'ar**: Pre-pull ranged stay below West platform with `nc +stay`; use Warrior/Druid tanks to charge between platforms
- **Void Reaver**: 3+ tanks recommended; bots spread and auto-dodge orbs
- **Kael'thas**: Multi-phase; bots handle legendary weapon assignment in P2; Gravity Lapse (P5) causes bot pathfinding issues — some bots float and cannot attack

### Mount Hyjal + Black Temple *(Tier 10 in IP)*

**Mount Hyjal** — Completable; strategies coded and auto-apply on entry; no explicit `co +` command.

**Black Temple** — Partially completable:

| Boss | Notes |
|------|-------|
| All bosses up to Council | Completable without strategies |
| Council of Illidari | Hard; requires careful RTI marks |
| Illidan | Not currently killable with bots alone |

### Zul'Aman *(completable; strategies auto-apply on entry; no explicit `co +` command)*

Full boss-by-boss strategies coded. Key notes:
- **Akil'zon**: Raid collapses on main tank during Electrical Storm — bots handle this automatically
- **Nalorakk**: Two tanks required (troll form / bear form swap)
- **Jan'alai**: Manual RTI marking of adds is helpful

### Sunwell Plateau *(NOT completable)*

Cannot pass the first boss without strategies. **Do not attempt** — considered blocked content for bot groups.

---

## WotLK Raids

### Vault of Archavon

Enable strategy: `co +voa` (up to Emalon only)

### Naxxramas (WotLK version)

Enable strategy: `co +naxx` (auto on entry)

> **Exception:** Heigan the Unclean is not currently defeatable with bots due to dance mechanic pathfinding limitations.

### Obsidian Sanctum

Enable strategy: `co +wotlk-os` (functional up to OS+2; kill Vesperon first)

### Eye of Eternity

Enable strategy: `co +wotlk-eoe` (auto on entry)

### Ulduar *(all bosses except Algalon)*

Enable strategy: `co +ulduar` (auto on entry)

> **Caveat:** Recent AzerothCore updates changed many boss scripts; some Ulduar strategies may be broken. Check `docs/wikis/mod-playerbots-wiki/Playerbot-Raid-Completion-Status.md` for current status.

### Onyxia's Lair

Enable strategy: `co +onyxia` (auto on entry)

### Icecrown Citadel

Enable strategy: `co +icc` (auto on entry)

### Trial of the Crusader *(WIP — needs strategies)*

Not fully completable. No bot strategies currently implemented.

### Ruby Sanctum *(unknown completability)*

Completability with bots has not been confirmed. The only WotLK content unlocked at IP Tier 17.

---

## Useful Raid Commands

### Set priority target (skull/cross/etc.)
```
rti skull       # Bots focus kill on skull-marked target
rti cc moon     # Bots CC moon-marked target (default)
attack rti target   # Command bots to attack their RTI target
```

### Control cooldowns
```
co -boost       # Disable major cooldowns (save for boss phase)
co +boost       # Activate cooldowns (burn phase)
```

### Position specific bots
```
rtsc            # Enable RTSC (gives you "aedm" spell)
rtsc save 1     # Save location 1 where you click with aedm
@tank rtsc go 1 # Send all tanks to saved location 1
```

### Handle adds
```
rti skull       # Mark kill target
rti cross       # Secondary target (optional)
```

### RTSC Reference Card
1. `/w self rtsc` — activate RTSC
2. Click with aedm spell on a spot to save it
3. `/w self rtsc save 1` — save that spot as location 1
4. `@group2 rtsc go 1` — send group 2 to location 1

---

## Performance Notes for Raids

- Keep bot count manageable: 5-10 bots per instance is fine for performance
- Use `co +assist` to ensure bots focus on the same target as each other
- In multi-phase bosses, use `co -boost` before the burn phase and `co +boost` to trigger it
- If bots are stuck or not responding, use `reset` to reset their current action
