# mod_ahbot.conf Reference
> Source of truth: `docs/configs/mod_ahbot.conf.dist`
> Key setup note: AuctionHouseBot.GUIDs is managed by the installer (Phase 6.1.4) and is BLOCKED in the admin UI — do not edit manually.
> GM commands: .ahbot reload | .ahbot empty | .ahbot update

---

## GENERAL / CORE SETTINGS

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.DEBUG | false | Enable debug output |
| AuctionHouseBot.DEBUG_FILTERS | false | Enable debug output from filters |
| AuctionHouseBot.MinutesBetweenBuyCycle | 1 | Minutes (or X:Y range) between buyer bot cycles |
| AuctionHouseBot.MinutesBetweenSellCycle | 1 | Minutes (or X:Y range) between seller bot cycles |
| AuctionHouseBot.EnableSeller | false | Enable the seller bot that posts items for auction |
| AuctionHouseBot.ReturnExpiredAuctionItemsToBot | false | Return expired bot auctions to bot via mail (can bloat mailbox and item_instance table) |
| AuctionHouseBot.GUIDs | 0 | Comma-separated character GUIDs used for AH operations; INSTALLER-MANAGED, do not edit |
| AuctionHouseBot.ItemsPerCycle | 150 | Items posted per seller update cycle; posted by randomly selected bot |
| AuctionHouseBot.ListingExpireTimeInSecondsMin | 900 | Minimum listing duration in seconds (15 min floor) |
| AuctionHouseBot.ListingExpireTimeInSecondsMax | 86400 | Maximum listing duration in seconds (48 hr ceiling) |

---

## COMPLETE ITEM VALUE OVERRIDE

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.CompleteItemValueOverride.Enabled | false | Enable fixed-price overrides that bypass all price calculations |
| AuctionHouseBot.CompleteItemValueOverride.Items | _(empty)_ | Comma-separated `itemID:PriceMinCopper` list of exact per-item prices |
| AuctionHouseBot.CompleteItemValueOverride.DoApplyBidVariations | false | Whether BidVariation* settings still apply to override-priced items |
| AuctionHouseBot.CompleteItemValueOverride.DoApplyBuyoutVariations | false | Whether BuyoutVariation* settings still apply to override-priced items |

---

## ADVANCED LISTING RULES — DROP RATE-BASED LISTING

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.AdvancedListingRules.UseDropRates.Enabled | false | Use in-game drop rates to influence how often items appear on AH |
| AuctionHouseBot.AdvancedListingRules.UseDropRates.Weapon | true | Apply drop-rate listing to weapons (if Enabled) |
| AuctionHouseBot.AdvancedListingRules.UseDropRates.Armor | true | Apply drop-rate listing to armor (if Enabled) |
| AuctionHouseBot.AdvancedListingRules.UseDropRates.Recipe | true | Apply drop-rate listing to recipes (if Enabled) |
| AuctionHouseBot.AdvancedListingRules.UseDropRates.Weapon.AffectedQualities | 2,3,4,5 | Qualities affected for weapons (0=Poor…6=Heirloom) |
| AuctionHouseBot.AdvancedListingRules.UseDropRates.Armor.AffectedQualities | 2,3,4,5 | Qualities affected for armor |
| AuctionHouseBot.AdvancedListingRules.UseDropRates.Recipe.AffectedQualities | 2,3,4,5 | Qualities affected for recipes |
| AuctionHouseBot.AdvancedListingRules.UseDropRates.MinDropRate | 0.005 | Minimum effective drop rate; raises ultra-rare items above this floor |
| AuctionHouseBot.AdvancedListingRules.UseDropRates.TiersConfig | 50,10,5,2,1,0.5,0.2,0.1,0.05,0.02,0.01,0.005 | Comma-separated tier thresholds for classifying drop-rate buckets |
| AuctionHouseBot.AdvancedListingRules.UseDropRates.DisabledItemIDs | _(empty)_ | Item IDs (or ranges with `-`) exempt from drop-rate listing logic |

---

## PRICE CAPS & VARIATION

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.MaxBuyoutPriceInCopper | 1000000000 | Hard cap on buyout price in copper (100k gold); decrease only |
| AuctionHouseBot.BuyoutVariationReducePercent | 0.15 | Random downward price variation (−15% from calculated price) |
| AuctionHouseBot.BuyoutVariationAddPercent | 0.25 | Random upward price variation (+25% from calculated price) |
| AuctionHouseBot.BidVariationHighReducePercent | 0 | Upper bound of bid discount below buyout (0 = bid can equal buyout) |
| AuctionHouseBot.BidVariationLowReducePercent | 0.25 | Lower bound of bid discount below buyout (−25% = bid can be 25% below buyout) |
| AuctionHouseBot.BuyoutBelowVendorVariationAddPercentEnabled | true | Apply vendor-floor markup when calculated buyout falls below vendor sell price |
| AuctionHouseBot.BuyoutBelowVendorVariationAddPercent | 0.25 | How much to add (+25%) when buyout is below vendor sell price |

---

## AUCTION HOUSE ITEM COUNTS

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.Alliance.MinItems | 15000 | Minimum simultaneous listings in the Alliance AH |
| AuctionHouseBot.Alliance.MaxItems | 15000 | Maximum simultaneous listings in the Alliance AH |
| AuctionHouseBot.Horde.MinItems | 15000 | Minimum simultaneous listings in the Horde AH |
| AuctionHouseBot.Horde.MaxItems | 15000 | Maximum simultaneous listings in the Horde AH |
| AuctionHouseBot.Neutral.MinItems | 15000 | Minimum simultaneous listings in the Neutral AH |
| AuctionHouseBot.Neutral.MaxItems | 15000 | Maximum simultaneous listings in the Neutral AH |

> Note: When `AllowTwoSide.Interaction.Auction` is enabled in worldserver.conf, only the Neutral AH appears in the DB; auctions still show in all AH windows.

---

## BUYER BOT

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.Buyer.Enabled | false | Enable the buyer bot that purchases player-listed items |
| AuctionHouseBot.Buyer.BuyCandidatesPerBuyCycle | 1 | Items per AH type (Alliance/Horde/Neutral) evaluated per cycle; accepts X:Y range |
| AuctionHouseBot.Buyer.AcceptablePriceModifier | 1 | Multiplier on bot's calculated price determining max willingness to pay |
| AuctionHouseBot.Buyer.AlwaysBidMaxCalculatedPrice | false | Bid the full calculated max price rather than minimum bid amount |
| AuctionHouseBot.Buyer.PreventOverpayingForVendorItems | true | Skip items listed above vendor sell price to prevent vendor-flip exploitation |
| AuctionHouseBot.Buyer.BidAgainstPlayers | false | Place competing bids on auctions players have already bid on |

---

## LIST PROPORTIONS (CATEGORY × QUALITY)

> Pattern: `AuctionHouseBot.ListProportion.Category<Cat>.Quality<Qual> = <weight>`
> Qualities: Poor, Normal, Uncommon, Rare, Epic, Legendary, Artifact, Heirloom
> Categories: Consumable, Container, Weapon, Gem, Armor, Reagent, Projectile, TradeGood, Generic, Recipe, Quiver, Quest, Key, Misc, Glyph
> A weight of 0 means that category/quality combination never appears. All values are whole numbers; proportional weight among all non-zero entries determines selection probability.

**Default weights summary (Notable values — zeros omitted for brevity):**

| Category | Poor | Normal | Uncommon | Rare | Epic |
|----------|------|--------|----------|------|------|
| Consumable | 0 | 50 | 10 | 5 | 0 |
| Container | 0 | 20 | 8 | 6 | 3 |
| Weapon | 0 | 10 | 40 | 15 | 4 |
| Gem | 0 | 0 | 20 | 8 | 2 |
| Armor | 10 | 20 | 50 | 25 | 8 |
| Reagent | 0 | 10 | 0 | 0 | 0 |
| Projectile | 0 | 10 | 6 | 4 | 2 |
| TradeGood | 0 | 80 | 15 | 8 | 2 |
| Generic | 10 | 10 | 10 | 10 | 10 |
| Recipe | 0 | 30 | 40 | 15 | 5 |
| Quiver | 0 | 10 | 8 | 3 | 0 |
| Quest | 0 | 25 | 3 | 2 | 1 |
| Key | 0 | 5 | 2 | 0 | 0 |
| Misc | 0 | 2 | 2 | 1 | 1 |
| Glyph | 0 | 30 | 0 | 0 | 0 |

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.ListProportion.ListMultipliedItemIDs | _(long list)_ | `itemID:multiplier` pairs to list specific items N× more often per cycle (cloth, ore, herbs, potions at 10×; consumables at 5× by default) |

---

## PRICE MINIMUM CENTER BASE

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.PriceMinimumCenterBase.UseItemSellPriceIfHigher | true | Use `item_template.SellPrice` as floor when it exceeds the configured category minimum |
| AuctionHouseBot.PriceMinimumCenterBase.OverrideItems | _(empty)_ | Per-item `itemID:PriceMinCopper` overrides of the category minimum before multipliers |

**Per-category minimums (in copper, before multipliers):**

| Key | Default |
|-----|---------|
| AuctionHouseBot.PriceMinimumCenterBase.Consumable | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Container | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Weapon | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Gem | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Armor | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Reagent | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Projectile | 5 |
| AuctionHouseBot.PriceMinimumCenterBase.TradeGood | 850 |
| AuctionHouseBot.PriceMinimumCenterBase.Generic | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Recipe | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Quiver | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Quest | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Key | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Misc | 1000 |
| AuctionHouseBot.PriceMinimumCenterBase.Glyph | 1000 |

---

## ADVANCED PRICING (LOGARITHMIC SUBCLASS PRICING)

> Pattern: `AuctionHouseBot.AdvancedPricing.<Category>.<Subclass>.Enabled = true/false`
> When enabled for a subclass, item-level pricing is disabled for that category. All multipliers are still applied multiplicatively on top.

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.AdvancedPricing.Consumable.Potion.Enabled | true | Logarithmic pricing for potions |
| AuctionHouseBot.AdvancedPricing.Consumable.Elixir.Enabled | true | Logarithmic pricing for elixirs |
| AuctionHouseBot.AdvancedPricing.Consumable.Flask.Enabled | true | Logarithmic pricing for flasks |
| AuctionHouseBot.AdvancedPricing.Gem.Enabled | true | Logarithmic pricing for gems |
| AuctionHouseBot.AdvancedPricing.TradeGood.Cloth.Enabled | true | Logarithmic pricing for cloth |
| AuctionHouseBot.AdvancedPricing.TradeGood.Herb.Enabled | true | Logarithmic pricing for herbs |
| AuctionHouseBot.AdvancedPricing.TradeGood.MetalStone.Enabled | true | Logarithmic pricing for metal/stone |
| AuctionHouseBot.AdvancedPricing.TradeGood.Leather.Enabled | true | Logarithmic pricing for leather |
| AuctionHouseBot.AdvancedPricing.TradeGood.Enchanting.Enabled | true | Logarithmic pricing for enchanting materials |
| AuctionHouseBot.AdvancedPricing.TradeGood.Elemental.Enabled | true | Logarithmic pricing for elemental trade goods |
| AuctionHouseBot.AdvancedPricing.TradeGood.Meat.Enabled | true | Logarithmic pricing for meat |
| AuctionHouseBot.AdvancedPricing.Misc.Junk.Enabled | true | Logarithmic pricing for junk |
| AuctionHouseBot.AdvancedPricing.Misc.Mount.Enabled | true | Logarithmic pricing for mounts |
| AuctionHouseBot.AdvancedPricing.Misc.Pet.Enabled | true | Logarithmic pricing for pets |

---

## PRICE MULTIPLIERS

> All multipliers are applied multiplicatively. Example: Category 1.5× × Quality 2× × CategoryQuality 1.4× = 4.2× final.

### Broad Category Multipliers

> Pattern: `AuctionHouseBot.PriceMultiplier.Category.<Cat> = <value>` (all default 1)
> Categories: Consumable, Container, Weapon, Gem, Armor, Reagent, Projectile, TradeGood, Generic, Recipe, Quiver, Quest, Key, Misc, Glyph

### Broad Quality Multipliers

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.PriceMultiplier.Quality.Poor | 1 | Multiplier for grey quality items |
| AuctionHouseBot.PriceMultiplier.Quality.Normal | 1 | Multiplier for white quality items |
| AuctionHouseBot.PriceMultiplier.Quality.Uncommon | 1.8 | Multiplier for green quality items |
| AuctionHouseBot.PriceMultiplier.Quality.Rare | 1.9 | Multiplier for blue quality items |
| AuctionHouseBot.PriceMultiplier.Quality.Epic | 2.1 | Multiplier for purple quality items |
| AuctionHouseBot.PriceMultiplier.Quality.Legendary | 3 | Multiplier for orange quality items |
| AuctionHouseBot.PriceMultiplier.Quality.Artifact | 3 | Multiplier for artifact quality items |
| AuctionHouseBot.PriceMultiplier.Quality.Heirloom | 3 | Multiplier for heirloom quality items |

### Item Level Multipliers (disabled by default)

> Pattern: `AuctionHouseBot.PriceMultiplier.ItemLevel.Category.<Cat> = 0`
> Final multiplier = itemLevel × this value. Set to 0 to disable. Ignored for any category where AdvancedPricing is enabled.
> All categories default to 0 (disabled): Consumable, Container, Weapon, Gem, Armor, Reagent, Projectile, TradeGood, Generic, Recipe, Quiver, Quest, Key, Misc, Glyph

### Fine-Tuning Category×Quality Multipliers

> Pattern: `AuctionHouseBot.PriceMultiplier.Category<Cat>.Quality<Qual> = <value>`
> 15 categories × 8 qualities = 120 keys. Applied in addition to broad Category and Quality multipliers.

**Notable non-1.0 defaults:**

| Key | Default | Notes |
|-----|---------|-------|
| AuctionHouseBot.PriceMultiplier.CategoryContainer.QualityNormal | 1.6 | White bags |
| AuctionHouseBot.PriceMultiplier.CategoryContainer.QualityUncommon | 6.0 | Green bags |
| AuctionHouseBot.PriceMultiplier.CategoryWeapon.QualityUncommon | 1.2 | Green weapons |
| AuctionHouseBot.PriceMultiplier.CategoryWeapon.QualityRare | 2.5 | Blue weapons |
| AuctionHouseBot.PriceMultiplier.CategoryWeapon.QualityEpic | 3.0 | Purple weapons |
| AuctionHouseBot.PriceMultiplier.CategoryGem.QualityUncommon | 1.2 | Green gems |
| AuctionHouseBot.PriceMultiplier.CategoryGem.QualityRare | 1.2 | Blue gems |
| AuctionHouseBot.PriceMultiplier.CategoryGem.QualityEpic | 0.9 | Purple gems (slightly below base) |
| AuctionHouseBot.PriceMultiplier.CategoryArmor.QualityUncommon | 1.2 | Green armor |
| AuctionHouseBot.PriceMultiplier.CategoryArmor.QualityRare | 2.5 | Blue armor |
| AuctionHouseBot.PriceMultiplier.CategoryArmor.QualityEpic | 3.0 | Purple armor |
| AuctionHouseBot.PriceMultiplier.CategoryReagent.QualityNormal | 1.6 | White reagents |
| AuctionHouseBot.PriceMultiplier.CategoryProjectile.QualityNormal | 0.9 | White ammo |
| AuctionHouseBot.PriceMultiplier.CategoryProjectile.QualityUncommon | 0.8 | Green ammo |
| AuctionHouseBot.PriceMultiplier.CategoryProjectile.QualityRare | 2.5 | Blue ammo |
| AuctionHouseBot.PriceMultiplier.CategoryProjectile.QualityEpic | 2.8 | Purple ammo |
| AuctionHouseBot.PriceMultiplier.CategoryTradeGood.QualityNormal | 0.8 | White trade goods |
| AuctionHouseBot.PriceMultiplier.CategoryTradeGood.QualityUncommon | 1.2 | Green trade goods |
| AuctionHouseBot.PriceMultiplier.CategoryTradeGood.QualityRare | 1.2 | Blue trade goods |
| AuctionHouseBot.PriceMultiplier.CategoryTradeGood.QualityEpic | 0.9 | Purple trade goods |
| AuctionHouseBot.PriceMultiplier.CategoryTradeGood.QualityLegendary | 1.4 | Orange trade goods |
| AuctionHouseBot.PriceMultiplier.CategoryRecipe.QualityUncommon | 2.0 | Green recipes |
| AuctionHouseBot.PriceMultiplier.CategoryRecipe.QualityRare | 2.0 | Blue recipes |
| AuctionHouseBot.PriceMultiplier.CategoryRecipe.QualityEpic | 20.0 | Purple recipes (high premium) |
| AuctionHouseBot.PriceMultiplier.CategoryQuest.QualityRare | 7.0 | Blue quest items |
| AuctionHouseBot.PriceMultiplier.CategoryQuest.QualityEpic | 8.0 | Purple quest items |
| AuctionHouseBot.PriceMultiplier.CategoryGlyph.QualityNormal | 14.0 | White glyphs (high premium) |
| AuctionHouseBot.PriceMultiplier.CategoryMount.QualityRare | 3000.0 | Blue mounts |
| AuctionHouseBot.PriceMultiplier.CategoryMount.QualityEpic | 5750.0 | Epic mounts |

> Additional category multiplier tables for CategoryMount, CategoryPet, CategoryMoney, CategoryPermanent exist (all default 1.0).

---

## LISTING STACK CONFIGURATION

> Three parallel patterns per category (15 categories each):
> - `AuctionHouseBot.ListingStack.RandomRatio.<Cat>` — 0–100; chance a listing uses random stack size vs. single
> - `AuctionHouseBot.ListingStack.RandomStackIncrement.<Cat>` — step size for random stacks (e.g. 5 = stacks of 5, 10, 15…)
> - `AuctionHouseBot.ListingStack.MaxStackSize.<Cat>` — custom max stack cap (0 = use item's native max)

| Category | RandomRatio | RandomStackIncrement | MaxStackSize |
|----------|-------------|---------------------|--------------|
| Consumable | 50 | 5 | 0 |
| Container | 0 | 1 | 0 |
| Weapon | 0 | 1 | 0 |
| Gem | 30 | 1 | 0 |
| Armor | 0 | 1 | 0 |
| Reagent | 50 | 1 | 0 |
| Projectile | 100 | 1000 | 0 |
| TradeGood | 75 | 5 | 0 |
| Generic | 100 | 1 | 0 |
| Recipe | 0 | 1 | 0 |
| Quiver | 0 | 1 | 0 |
| Quest | 10 | 1 | 0 |
| Key | 10 | 1 | 0 |
| Misc | 100 | 1 | 0 |
| Glyph | 0 | 1 | 0 |

> Tip: Set RandomRatio=100 and RandomStackIncrement >= max item stack size to always post full stacks (default for Projectile).

---

## ITEM LEVEL RESTRICTIONS

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.ListedItemLevelRestrict.Enabled | false | Enable item level filtering on seller listings |
| AuctionHouseBot.ListedItemLevelRestrict.MinItemLevel | 0 | Minimum item level to list |
| AuctionHouseBot.ListedItemLevelRestrict.MaxItemLevel | 999 | Maximum item level to list |
| AuctionHouseBot.ListedItemLevelRestrict.UseCraftedItemForCalculation | true | For recipes, evaluate the produced item's level instead of the recipe item's level |
| AuctionHouseBot.ListedItemLevelRestrict.ExceptionItemIDs | _(empty)_ | Item IDs (or ranges with `-`) exempt from item level restriction |

---

## EQUIP / USE LEVEL RESTRICTIONS

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.EquipItemUseOrEquipLevelRestrict.Enabled | false | Enable equip/use level filtering on seller listings |
| AuctionHouseBot.EquipItemUseOrEquipLevelRestrict.MinLevel | 0 | Minimum equip or use level to list (items with no level are never restricted) |
| AuctionHouseBot.EquipItemUseOrEquipLevelRestrict.MaxLevel | 999 | Maximum equip or use level to list |
| AuctionHouseBot.EquipItemUseOrEquipLevelRestrict.ExceptionItemIDs | _(empty)_ | Item IDs (or ranges with `-`) exempt from equip/use level restriction |

---

## ITEM ID RESTRICTIONS

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.ListedItemIDRestrict.Enabled | false | Enable item ID range filtering on seller listings |
| AuctionHouseBot.ListedItemIDRestrict.MinItemID | 0 | Minimum item entry ID to list |
| AuctionHouseBot.ListedItemIDRestrict.MaxItemID | 200000 | Maximum item entry ID to list |
| AuctionHouseBot.ListedItemIDRestrict.ExceptionItemIDs | _(empty)_ | Item IDs (or ranges with `-`) exempt from ID restriction |

---

## ITEM FILTERING / DISABLED ITEMS

| Key | Default | Description |
|-----|---------|-------------|
| AuctionHouseBot.DisabledItemTextFilter | true | Suppress listings of items with test/invalid names (e.g. "OLD", "D'Sak") |
| AuctionHouseBot.DisabledRecipeProducedItemFilterEnabled | false | Enable filtering recipes by the class/subclass of the item they produce |
| AuctionHouseBot.DisabledRecipeProducedItemClassSubClasses | 2:*,4:*,15:2,15:5 | Class:subclass pairs (wildcard `*` for subclass) to filter; defaults block player-crafted weapons, armor, pets, mounts |
| AuctionHouseBot.DisabledInvalidItemIDs | _(long list)_ | Known unusable/unobtainable item IDs never listed; maintained by mod-ah-bot-plus |
| AuctionHouseBot.DisabledCustomItemIDs | _(empty)_ | Admin-defined extra item IDs to exclude from listings |
