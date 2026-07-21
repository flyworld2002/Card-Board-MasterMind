# Plan: DB → eBay price/qty listing sync

## BUILD SESSION STATUS (2026-07-21)
**Both blockers resolved by Fei via handoff addendum. Migrations applied.
Proceeding to recalc engine, push engine, main.py flags.**

- ✅ Done (unattended pass): live-DB introspection, RPC cross-repo grep,
  migration 001 (4 new tables + `platform_listings` columns `template_id`,
  `base_price`, `sync_enabled`, `price_synced_at`, `updated_at` + trigger;
  `set_pricing_config.tier_bump`; `card_pricing_overrides.quantity_limit`),
  shared XML helper module `importer/ebay_variations_xml.py` (extracted
  from `rename_variation.py`, verified still working).
- ✅ **Finding #8 RESOLVED**: `card_type_mapping` rebuilt with Fei's
  `rarity` + `variant_key` (LIKE-pattern) schema — see "Step 0 — Finding #8
  resolution" below. Seeded with the 10 confirmed rows (8 core rarities +
  1 reverse-holo override + 1 wildcard). The other 8 rarity strings found
  live (Double Rare, Shiny Rare, ACE SPEC Rare, Promo, Shiny Ultra Rare,
  Mega Hyper Rare, NULL, MEGA_ATTACK_RARE) are deliberately left unseeded
  — **Fei wants these configured by hand via a Configuration UI
  `card_type_mapping` editor** (not yet built — new required web-repo
  deliverable, see "Files expected to change"). Until that editor exists
  and Fei populates them, cards with those rarities resolve via the
  wildcard → `'common'` — **the recalc engine's `--dry-run` MUST flag
  every card that resolved via the wildcard (specificity score 0)** so
  these don't silently under-price.
- ✅ **Finding #9 RESOLVED**: extended the existing `listing_templates`
  table in place (did not rename/replace). Dumped full schema + both rows
  verbatim before altering (see "Step 0 — Finding #9 resolution" below).
  Added 9 new nullable/defaulted columns, backfilled `commons` and
  `reverse_holo_ultra` with `priority_rule`/`display_sort`/`base_price`.
  Existing `included_types`/`excluded_types`/`card_num_min/max`/
  `shipping_base`/`shipping_per_card`/`max_quantity` left untouched —
  still live in the Configuration UI. `card_type_filter` left NULL on both
  rows (see resolution notes — mapping the old rarity-string arrays onto
  the new tier_card_type domain isn't a clean 1:1, flagged rather than
  guessed).
- ✅ **Step 0 #7 RESOLVED**: Fei confirmed `display_sort` is a plain
  single-key sort (numeric/alpha/release-date), no band-grouping needed —
  `reverse_holo_ultra`'s `display_sort='card_number'` means interleaved by
  card number, not RH-blocked-first. `release_date` added as a third
  reserved `display_sort` value for the future cross-set Pokémon-themed
  listings.
- ✅ **`--ebay-recalc-prices` built and working**
  (`importer/ebay_listing_sync.py`: `resolve_tier_card_type`,
  `resolve_price`, `recalc`) — full pipeline (override → tier resolver →
  tier bracket + bumps → set multiplier → floor → rounding), never-lower
  guard with correct bump-expiry detection (computes a
  `list_price_bump_forced` counterfactual and compares against the stored
  price, rather than blindly blocking every decrease), gating through
  `platform_sync_status` → `sync_enabled` → `status='active'`. Verified
  against real data: `resolve_tier_card_type` correctly routes
  Illustration Rare / Ultra Rare / Hyper Rare → `ultra_rare_rule`
  regardless of `foil_type` (the exact gap finding #8 identified), and
  correctly flags Shiny Rare / Double Rare as wildcard-tier in the
  breakdown. `_resolve_scope` verified end-to-end via an explicit
  rollback-only transaction test (no real listing was enabled — flipping
  `sync_enabled` on a real listing is explicitly Fei's action per the
  Rollout sequence, not something to do here). Wired to `main.py` as
  `--ebay-recalc-prices` with `--item-id` / `--card` / `--dry-run` /
  `--quiet` / `--allow-decreases`; smoke-tested via the real CLI.
- ✅ **`--ebay-push-listings` built** (`_push_single`, `_push_variation_listing`,
  `_compute_desired_qty`, `_get_listing_kind`, `_render_variation_name`,
  `_compute_insert_position`). Both `listing_kind` paths implemented: plain
  item-level revise for `'single'`, full `GetItem → reconcile against
  listing_card_assignments → ReviseFixedPriceItem` for `'variation'`
  (including 250-cap promotion: delete a sold-out row via
  `mark_variation_deleted`, promote the highest-priority queued
  assignment, insert its rendered name into `VariationSpecificsSet` at the
  correct `display_sort` position). Qty push formula correctly uses
  `QuantitySold` read from the *original* fetched XML before
  `strip_selling_status` removes it (Step 0 #6). Push skips (does not
  error) any listing with zero `listing_card_assignments` rows — per the
  locked design, it never touches a listing this tool hasn't onboarded.
  **Schema correction found while implementing this**: `platform_listing_id`
  had to become nullable + a new `ebay_item_id` column added, because a
  `'queued'` (not-yet-live) assignment has no `platform_listings` row to
  reference yet — see the `listing_card_assignments` DDL section above.
  Table was empty, so purely additive, no data impact.
  **Naming-engine bug caught and fixed during testing**: the initial
  `_render_variation_name` didn't factor `foil_type` into the suffix at
  all, so a holo and reverse-holo copy of the same card would have
  rendered to an *identical* `VariationSpecificsSet` value — checked
  against real listings (e.g. item 334903449758) and confirmed reverse-holo
  variants always carry a "Reverse Holo RH" suffix live; fixed to always
  include that plus humanized `foil_pattern`/`stamp_type` text.
  **What's tested vs. not**: all pure DB-facing helpers
  (`_compute_desired_qty`, `_get_listing_kind`, `_render_variation_name`)
  verified against real live data (read-only). `_resolve_scope` verified
  via a rollback-only transaction. The full CLI path
  (`--ebay-push-listings --dry-run`) smoke-tested and behaves correctly
  for an empty/no-op scope. **The actual GetItem/ReviseFixedPriceItem
  call path, and the 250-cap promotion logic specifically, have NOT been
  exercised against a real eBay listing** — that requires Rollout steps 2–3
  (backfill `listing_card_assignments` for a test listing, flip
  `sync_enabled` on) which are explicitly Fei's actions, not something to
  do unprompted against real listings/data.
- ✅ **Configuration UI (web repo) built**:
  - `card_type_mapping` editor — new "Card type mapping" tab under
    Pricing rules (`configuration.js`), full CRUD (platform, account,
    rarity, variant_key pattern, tier, priority). This is the self-serve
    editor Fei asked for to configure the unmapped rarities (Double Rare,
    Shiny Rare, etc.) himself.
  - `listing_templates` screen extended in place with all 9 new columns
    (listing_kind, base_price, default_quantity_limit,
    low_stock_threshold/bump, priority_rule, display_sort, name_format,
    card_type_filter) — existing fields/rows untouched.
  - New "Sync controls" configuration section for `platform_sync_status`
    kill switches (platform-wide or per-account freeze).
  - `inventory.js`: added a `sync_enabled` checkbox + template dropdown to
    both existing listing-edit UIs (the inline detail-panel editor and the
    separate listings modal — this repo has two parallel edit surfaces for
    the same `platform_listings` row; both were updated for consistency).
    Added a "bulk enable in set" button that appears when the inventory
    set filter is active — resolves all eBay listings for that set's
    variants and turns `sync_enabled` on for any not already enabled,
    after a confirm showing the count. **Fixed a wiring gap while adding
    this**: the set-filter dropdown's change handler wasn't re-invoking
    `renderFilters`, so the new button wouldn't have appeared/disappeared
    when switching sets — added the missing re-render call.
  - Not syntax-verified by an actual JS runtime (none available in this
    environment) — verified via brace/paren/backtick balance checks and
    careful manual re-read instead. Should be smoke-tested in the browser
    before relying on it for a real Rollout step 3.
- ⏳ Not started: `listing_card_assignments` backfill (Rollout sequence
  step 2) for any real listing.

## Session context
This was a `/plan`-only conversation. No code written, no migrations applied.
This doc is the full agreed design, ready to execute in a build session.
All decisions below were made explicitly with Fei — do not re-litigate them;
remaining unknowns are collected in "Step 0" only.

## Feature summary
A new one-way sync pushing **price and quantity** from Card-Board-MasterMind
to **existing live eBay multi-variation listings**, gated by a per-LISTING
allowlist (`platform_listings.sync_enabled`), run manually while trust
builds. Two-phase architecture: **Recalculate** (pricing pipeline →
`platform_listings.list_price`, DB-only) then **Push** (`platform_listings`
→ eBay via `ReviseFixedPriceItem`). Includes variation add/remove to
manage eBay's 250-variation cap with a priority-based holdback queue.

Explicitly OUT of scope: creating new listings, title/description/image
changes, scheduling/automation, trigger-on-sale (future), desirability
rating (future, slot reserved in pipeline).

## Locked decisions
- **Direction**: DB → eBay only. Price + qty. Existing listings only.
- **Pricing source of truth**: `platform_listings.list_price` (the platform
  pricing layer). `inventory.asking_price` is NEVER touched by this feature.
- **Recalc vs push are separate commands** — recalc writes DB only; push
  reads DB and calls eBay. `synced_at` vs recalc `updated_at` = "pending
  changes" signal.
- **Sync gating**: per-LISTING allowlist via `platform_listings.sync_enabled`
  (default false — a listing doesn't sync until explicitly turned on).
  This is the rollout dial: start with 1 listing on, grow to all as trust
  builds. Configuration UI includes a "bulk enable in set" action so you
  can flip a set's worth of listings on together without a set-level
  schema. Plus a platform-level kill switch (`platform_sync_status`
  table, one row per platform, `sync_enabled` default TRUE) — normally
  on, one flip freezes all sync on that marketplace at once. Gate order
  at sync time: platform switch → per-listing switch. **Manual scopes
  (`--item-id`, `--card`) respect the per-listing switch strictly** — a
  disabled listing is skipped even when explicitly targeted (safer;
  switch means one thing everywhere).
- **Always push qty; price only written when changed** (recalc compares
  resolved price to current `list_price`).
- **Trigger**: manual CLI only. Scopes: `--item-id` (one listing, must be
  enabled), `--card` (one variant across whichever enabled listings hold
  it). `--dry-run` at every scope. No scheduler wiring. `--set` scope
  deliberately deferred — add later if bulk-by-set targeting is actually
  needed.
- **Quantity formula**:
  `MIN(quantity_available, COALESCE(card_override.quantity_limit,
  listing.quantity_limit, template.default_quantity_limit, 24))`
  (all platform-scoped).
- **eBay mechanics**: `ReviseFixedPriceItem` (CONFIRMED live 7/20 — see
  Step 0 #2). Also confirm `rename_variation.py` uses the same call and
  extract its XML-handling into a shared helper. Uses the
  `rename_variation.py` pattern (deep-copy `<Variations>`, strip
  `<SellingStatus>`). This tool OWNS the full variations block: it may
  add `<Variation>` rows (adding to `VariationSpecificsSet` when the name
  isn't in the menu) and remove rows. `VariationSpecificsSet` entries
  are never deleted (menu = history).
- **250-cap holdback**: when a listing's intended variants exceed 250 and a
  live variation sells out, delete its row (CONFIRMED viable via live API
  test 7/20 — see Step 0 #2) and promote the highest-priority queued card.
  Under 250, sold-out rows always stay (qty 0, hidden, harmless) — no
  deletion needed unless promoting.
- **Priority order for slots** (RH-to-UR listing archetype): reverse holo
  first → others by card number → regular holo last — preset
  `'rh_then_number_holo_last'` on the listing template, not hardcoded.
  **Promotion priority and buyer-facing display order are two separate
  template settings** (`priority_rule` vs `display_sort`) — a card can be
  high-priority for a slot yet still display in numeric position.
- **Never-lower guard**: sync won't decrease a live price without
  `--allow-decreases` — EXCEPT when the decrease is caused solely by a
  low-stock bump expiring (restock crossed back above threshold); that
  un-bump is legitimate and always allowed. Implementation (no history
  needed): at recalc, compute the resolved price both with and without
  bumps; a decrease is auto-permitted iff current `list_price` equals the
  with-bump value and the new price equals the without-bump value.

## Pricing resolution pipeline (locked)
Per (card, listing), platform + account scoped
(`platform='ebay' AND (account=X OR account IS NULL)`):

```
1. card_pricing_overrides.list_price   — absolute; beats everything incl. floors
2. card_type_mapping (rarity + variant_key, most-specific-first — revised
   per Step 0 finding #8, see that section for the resolver query) →
   tier type OR formula path. --dry-run flags any card resolved via the
   specificity-0 wildcard row (see finding #8 resolution).
3a. TIER PATH: price_tiers bracket lookup
      (first tier by sort_order with market_price_max >= market price)
    then tier bumps, stacking:
      + set_pricing_config.tier_bump          (rotation; set-wide, manual)
      + low-stock bump                        (qty < threshold; from template,
                                               overridable per listing)
      clamp at top tier (report "clamped" in dry-run)
3b. FORMULA PATH (ultra-rare rule / above top tier — market price exceeds the
    largest market_price_max): market × multiplier, or market + plus,
    per set_pricing_config.ultra_rare_rule. NO bumps in v1
    (report "bump n/a: formula-priced").
4. set_pricing_config.price_multiplier
5. [FUTURE SLOT: desirability scaling — per-card platform-scoped rating
    scaling price and qty; not built now, pipeline position reserved]
6. Floors — raise-only, highest wins; SKIPPED if price came from step 1:
      listing.base_price → template.base_price → set floor by card type
7. Rounding — applied whenever the final number did NOT come verbatim from
   a tier row (formula path, or tier × multiplier ≠ 1.0): up to nearest
   .49/.99. Untouched tier prices keep their exact configured endings.
8. Fallbacks:
      no market_prices row (or <= 0)  → set floor for its type;
                                        no floor → SKIP + report
      stale market price              → use as-is; show age in dry-run
```

## Data model changes (DDL sketches — verify column names in Step 0)

### Extended: `listing_templates` (existing table — see Step 0 findings
above for pre-migration schema + live row data)

Applied as ALTER statements against the real, already-populated table
(NOT a fresh CREATE — see finding #9 resolution):

```sql
ALTER TABLE listing_templates ADD COLUMN base_price numeric;
  -- listing-level raise-only floor. Backfilled: commons=0.99,
  -- reverse_holo_ultra=1.37 (both cross-checked against live price_tiers
  -- rungs for account BIGGYFISH — see finding #9 resolution).
ALTER TABLE listing_templates ADD COLUMN default_quantity_limit integer;
ALTER TABLE listing_templates ADD COLUMN low_stock_threshold integer DEFAULT 8;
ALTER TABLE listing_templates ADD COLUMN low_stock_bump integer DEFAULT 1;
ALTER TABLE listing_templates ADD COLUMN listing_kind text NOT NULL DEFAULT 'variation';
ALTER TABLE listing_templates ADD COLUMN priority_rule text DEFAULT 'card_number';
  -- promotion-priority preset: 'card_number' (plain numeric) |
  -- 'rh_then_number_holo_last' (RH first -> card number -> regular holo last)
ALTER TABLE listing_templates ADD COLUMN display_sort text DEFAULT 'card_number';
  -- DISPLAY order (buyer-facing dropdown), SEPARATE from promotion
  -- priority: 'card_number' | 'alpha' | 'release_date' (reserved for the
  -- future cross-set themed-listings feature). Confirmed a plain
  -- single-key sort — no band-grouping support needed (Step 0 #7 resolved).
ALTER TABLE listing_templates ADD COLUMN name_format text DEFAULT '{number}/{set_total} {name} {suffix}';
  -- Tokens: {number}, {number:pad} (padded to card_sets.total_cards width),
  -- {set_total} (= card_sets.total_cards, NOT a new official_set_total
  -- column — see finding #5), {name}, {suffix}.
ALTER TABLE listing_templates ADD COLUMN card_type_filter text[];
  -- Left NULL on both existing rows deliberately — see finding #9
  -- resolution for why this isn't auto-derived from included_types.
ALTER TABLE listing_templates ADD COLUMN updated_at timestamptz DEFAULT now();
-- platform, account already existed on the live table.
```

Existing columns (`name`, `description`, `included_types`, `excluded_types`,
`card_num_min`, `card_num_max`, `shipping_base`, `shipping_per_card`,
`max_quantity`) are untouched and still drive the live Configuration UI.

v1 scope guard: the new columns hold pricing/qty/priority defaults ONLY.
No title patterns or image rules (future create-listings feature).

### `card_type_mapping` (rarity + variant_key → pricing type)
**Schema revised from the original 7-axis design — see finding #8
resolution above for why and the full seed data.** Keys off `rarity`
(from `card_master`, primary signal) and `variant_key` (LIKE pattern
against `card_variants.variant_key`, secondary/override signal), both
nullable = wildcard:

```sql
CREATE TABLE card_type_mapping (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform text NOT NULL DEFAULT 'ebay',
  account text,
  rarity text,
  variant_key text,
  tier_card_type text NOT NULL,      -- 'common'|'holo'|'reverse_holo'|'ultra_rare_rule'
  priority integer NOT NULL DEFAULT 10,
  created_at timestamptz DEFAULT now()
);
```

Resolved most-specific-first via the query in finding #8's resolution
above. Seeded with the 10 confirmed rows there; the remaining real-world
rarities (Double Rare, Shiny Rare, etc.) are intentionally left for Fei to
add via a Configuration UI editor (not yet built).

### New: `platform_sync_status` (platform + account kill switch)
Emergency freeze for a whole platform or one account. Default-ON: rows
only exist to turn something OFF. Not the primary allowlist — that's
`platform_listings.sync_enabled` (per-listing).

```sql
CREATE TABLE platform_sync_status (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform text NOT NULL DEFAULT 'ebay',
  account text,                      -- NULL = platform-wide switch
  sync_enabled boolean NOT NULL DEFAULT true,
  disabled_at timestamptz,
  notes text,
  UNIQUE (platform, account)
);
```

### New: `listing_card_assignments` (intent layer for the 250-cap)
`ebay_listing_map` records observed live state; this table records INTENT —
which cards belong to which listing, in what priority, including cards not
currently live:

```sql
CREATE TABLE listing_card_assignments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform_listing_id uuid REFERENCES platform_listings(id),  -- nullable, see below
  variant_id uuid NOT NULL REFERENCES card_variants(id),
  priority_rank integer NOT NULL,    -- computed from template priority_rule
  status text NOT NULL DEFAULT 'queued',
    -- 'active' | 'queued' | 'sold_out_retained'
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  ebay_item_id text,                 -- see below
  UNIQUE (platform_listing_id, variant_id)
);
ALTER TABLE listing_card_assignments ADD CONSTRAINT chk_lca_has_listing_ref
  CHECK (platform_listing_id IS NOT NULL OR ebay_item_id IS NOT NULL);
```

**Correction made while implementing the push engine (2026-07-21)**: the
original `platform_listing_id uuid NOT NULL` doesn't actually work for
`status='queued'` rows — a queued card is, by definition, not live yet, so
it has no `platform_listings` row to reference (that row only gets created
once it's promoted to `'active'`). Made `platform_listing_id` nullable and
added `ebay_item_id` (text, the eBay ItemID — same convention as
`platform_listings.listing_id` / `ebay_listing_map.item_id`) so a queued
row can still identify which multi-variation listing it's queued for, with
a check constraint requiring at least one of the two. Table was empty
(0 rows, nothing backfilled yet per Rollout step 2) so this was a safe,
purely additive correction — not a breaking change to real data.

### Column additions — REVISED per live-DB findings above (Step 0 #1–5)

```sql
ALTER TABLE platform_listings ADD COLUMN template_id uuid
  REFERENCES listing_templates(id);          -- nullable; own fields override template
ALTER TABLE platform_listings ADD COLUMN base_price numeric;  -- per-listing override
ALTER TABLE platform_listings ADD COLUMN sync_enabled boolean NOT NULL DEFAULT false;
  -- ★ THE PRIMARY ALLOWLIST (rollout dial). Defaults OFF. Gate at sync
  -- time is: platform_sync_status switch -> sync_enabled -> status='active'
  -- (the last clause folds in the pre-existing 'do_not_sync'/'draft'/
  -- 'delisted' manual status values as a hard exclusion — see Step 0 #4).
ALTER TABLE platform_listings ADD COLUMN price_synced_at timestamptz;
  -- NEW column, deliberately NOT reusing the existing `synced_at` column
  -- (that one is already written by the live record_sale RPC on every
  -- ordinary sale — see Step 0 #3 — and reusing it would make unpushed
  -- price changes look already-synced). --push stamps price_synced_at
  -- on success; "pending changes" = updated_at > price_synced_at.
ALTER TABLE platform_listings ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
  -- Did NOT already exist (confirmed). Needs a trigger to bump on
  -- list_price updates (recalc writes) so price_synced_at comparison works.
-- NOTE: quantity_limit already exists on platform_listings (default 18) —
-- do NOT add it again, it already serves the "listing.quantity_limit"
-- fallback layer in the qty formula.
-- NOTE: synced_at already exists (see above) — left untouched, still
-- owned by record_sale; the new code must never write to it.
ALTER TABLE set_pricing_config ADD COLUMN tier_bump integer NOT NULL DEFAULT 0;
ALTER TABLE card_pricing_overrides ADD COLUMN quantity_limit integer;  -- per-card qty cap
-- REMOVED: card_sets.official_set_total — card_sets.total_cards already
-- serves this purpose (confirmed live in the Configuration UI's Set
-- editor, "Total cards" field, displayed as count/total_cards). Reuse it;
-- just backfill any sets where it's currently NULL instead of adding a
-- column. name_format's {set_total} / {number:pad} read total_cards.
```

COALESCE precedence everywhere: card override → listing → template → default.
⚠ Remember the `staging%ROWTYPE` gotcha: DROP/recreate any RPC touching
altered tables after migration. Specifically double-check `record_sale`
still works after `platform_listings` gets new columns (it uses
`UPDATE platform_listings SET ... WHERE platform=... AND listing_id=...
AND external_id=...` — adding columns doesn't break a targeted UPDATE, but
adding the `updated_at` trigger must not fire in a way that conflicts with
that statement).

## Sync engine flow

### `--recalc` (DB-only)
Flags: `--dry-run`, `--quiet` (one summary line per listing, matches the
existing `--ebay-pullorders --quiet` pattern), `--allow-decreases` (bypass
the never-lower guard), scope flags (`--item-id`, `--card`).

1. Resolve scope (--item-id / --card) → (card, listing) pairs, filtered
   to `platform_listings.sync_enabled=true` and
   `platform_sync_status.sync_enabled=true` for the platform/account.
2. Run pricing pipeline per pair.
3. Where resolved ≠ current `list_price`: update `platform_listings.list_price`
   (+ `updated_at`). Apply never-lower guard unless `--allow-decreases`.
4. Dry-run prints old→new per variation, bump reasons, clamps, skips, market
   price age. Quiet mode: one summary line per listing, `[timestamp]` style.

### `--push` (eBay writes)
Two engine paths, selected by `listing_templates.listing_kind`. The steps
below describe the **`variation`** path (the primary case). The
**`single`** path uses the same scope filter and success stamping (steps
1, 3, 4 below) but replaces step 2 with a plain item-level
`ReviseFixedPriceItem` setting `<StartPrice>` and `<Quantity>` at the
Item level (no `<Variations>` block, no specifics set, no 250 cap, no
promotion queue, one `listing_card_assignments` row per listing). Needed
for the planned themed single-card accounts.

**`variation` path steps:**

Flags: `--dry-run`, `--quiet` (one summary line per listing), `--force`
(push all listings in scope regardless of whether they have pending
changes), scope flags (`--item-id`, `--card`).

1. Scope → listings with pending changes (or all in scope with `--force`).
2. Per listing: `GetItem` → reconcile against `listing_card_assignments`:
   - Update price/qty on live variations from `platform_listings` /
     qty formula. **Qty push formula (CONFIRMED 7/20 — see Step 0 #6):
     `Quantity_to_set = QuantitySold (from this GetItem) +
     MIN(quantity_available, cap)`** — never push the bare available
     number, `<Quantity>` is cumulative-ever-listed, not remaining stock.
   - 250-cap promotion: if intended variants > 250 and a live variation is
     sold out, free its slot and ADD the highest-priority queued card.
     **`card_type_filter` enforcement**: applied only when auto-populating
     assignments from a rule (future feature) — the promotion step here
     just picks the top of the existing `listing_card_assignments` queue,
     which was already filtered at assignment time. Explicit human
     assignment rows always win regardless of filter.
     **Menu ordering matters**: eBay displays the dropdown in
     `VariationSpecificsSet` value order — a promoted card's name must be
     INSERTED at its correct `display_sort` position in the specifics set
     (name rendered via the template's `name_format`), never appended to
     the end, or promoted cards pile up at the bottom of the dropdown.
   - Under 250: no deletions ever; sold-out rows remain at qty 0.
   - **eBay deletion rule — CONFIRMED VIABLE 7/20 via live API test**
     (see Step 0 #2): despite docs stating purchased variations can never
     be deleted, `Variation.Delete=true` via `ReviseFixedPriceItem`
     successfully deleted a variation with 3 past sales from a live
     15-variation listing (15→14 rows, confirmed gone via GetItem, only
     a harmless unrelated SKU warning). Promotion via delete + add is the
     confirmed mechanism — no qty-0 fallback required.
3. `ReviseFixedPriceItem`; stamp `synced_at` on success.
4. Mismatch guard from `rename_variation.py` (specifics count vs. active
   rows) becomes RECONCILE here, not skip — but any listing this tool
   didn't build the assignment intent for yet should skip + warn.

## Step 0 — audit before writing any code
1. Confirm exact column names: `card_variants` 7 axis columns, `card_sets`
   PK, `market_prices` join key, `ebay_listing_map` shape,
   `set_pricing_config` (verified live 7/20 to exist with `price_multiplier`,
   type-specific floors, `ultra_rare_rule` — but re-check exact column names
   against the current schema), and whether `platform_listings` has an
   `updated_at` (change-time) column — if not, add it during migration with
   a trigger to bump on `list_price` updates.
2. **CONFIRMED 7/20 — variation deletion works via the API.** Live test:
   `Variation.Delete=true` via `ReviseFixedPriceItem` on item 335081047848
   (McDonald's Match & Battle 15-listing), deleting "007/015 Pawmi" — a
   variation WITH 3 past sales (7 units still available, not even sold
   out). Result: `Ack=Warning` (harmless "SKU missing in variation" notice,
   unrelated to the delete), confirmed via GetItem: **15 → 14 rows, fully
   gone** (not retained at qty 0). eBay's documented "can never delete a
   purchased variation" restriction is NOT enforced in practice via the
   Trading API for this account. Promotion (delete sold-out row + add
   queued card) is confirmed viable as originally designed — no qty-0
   fallback needed. Residual low-risk gap: not tested at exactly zero
   remaining stock, but given a variation WITH active stock deleted
   cleanly, a zero-stock one is expected to behave the same or better.
   Remaining Step 0 items below are still open.
3. Verify adding a brand-new value to `VariationSpecificsSet` on a live
   listing via `ReviseFixedPriceItem` passes eBay verification (Fei: "if
   eBay verification is okay with it, happy to go with it").
4. Confirm where the 7-axis combined text / per-axis fields live for the
   mapping join, and whether `platform_listings` ↔ variant ↔ eBay variation
   linkage is complete enough to build `listing_card_assignments` from
   existing data. Note the backfill process design lives in the Rollout
   sequence below (step 2) — Step 0 only confirms the join is feasible.
5. Locate any existing price-resolution code from the Catalog/Configuration
   work to reuse rather than reimplement.
6. **CONFIRMED 7/20 — Quantity semantics.** `<Quantity>` on a variation is
   TOTAL EVER LISTED, not remaining stock. Confirmed via GetItem on item
   335081047848: "007/015 Pawmi" showed `Quantity=10`, `QuantitySold=3`,
   i.e. 7 actually available — not 10. **This changes the qty-push
   formula**: pushing `quantity_available` from the DB straight into
   `<Quantity>` would be WRONG (would misrepresent/reset the sold count).
   Correct push value: `Quantity_to_set = QuantitySold (fetched live via
   GetItem) + desired_available_quantity`. The sync engine must fetch
   current `QuantitySold` per variation before every push, not just push
   a bare number from the DB. Fold this into the `--push` design (Sync
   engine flow section) before building.
7. **Confirm RH-to-UR dropdown organization with Fei**: interleaved by
   card number (RH `007` next to holo `007`) or blocked (all RH first)?
   Decides whether `display_sort` needs band support or stays a simple
   numeric/alpha choice. Ask before building the menu-insert logic.
8. Backfill plan for `card_sets.official_set_total` — how many sets need
   values, and the source (pokemontcg.io `printedTotal` vs. manual entry
   per set).

## Step 0 — LIVE DB FINDINGS (2026-07-21, build session, post-DB-introspection)
These supersede the "prep findings" subsection below and materially change
the DDL sketches and gating design above. Read this before writing any
migration.

1. **`platform_listings` already has more columns than the plan assumed**
   (13 total): `id, platform, external_id, list_price, status, listed_at,
   synced_at, variant_id, account, listing_id, quantity_limit,
   quantity_listed, description`.
   - `variant_id` already exists (direct FK-ish uuid, no join through
     `inventory` needed) — simplifies `listing_card_assignments` backfill.
   - `account` already exists — multi-account scoping is already there.
   - `quantity_limit` (int, **default 18**, not 24) already exists — this
     already covers the "listing.quantity_limit" fallback layer in the qty
     formula. **Do not add this column; it's already there** (only
     `card_pricing_overrides.quantity_limit` and
     `listing_templates.default_quantity_limit` are genuinely new).
   - `synced_at` already exists **and is already actively written by the
     live `record_sale` RPC** on every sale (see finding #3) — it is NOT a
     free column for the new push command's exclusive use. See finding #3
     for the conflict and the fix.
   - No `updated_at` column, confirming the original Step 0 #1 concern —
     still needs to be added with a trigger.
   - `listing_id` (text) and `external_id` (text) are BOTH populated and
     used together as the match key — see finding #2.

2. **`ebay_listing_map` is genuinely absent from `schema.sql`** (confirmed
   live-DB-only). Live columns: `id, item_id, variation_name, listing_id,
   last_synced_at, created_at, variant_id, source, condition`.
   `platform_listings.listing_id` = the eBay **ItemID**;
   `platform_listings.external_id` = the eBay **variation name** (or `''`
   for single-item listings) — confirmed via the live `record_sale` RPC
   body (finding #3). This mirrors `ebay_listing_map(item_id,
   variation_name)`. The sync engine should resolve/match rows the same
   way: `(platform, listing_id, external_id)` or via `variant_id` directly
   since that FK now exists on `platform_listings`.

3. **Fetched the live `record_sale` RPC source** (not in `schema.sql`;
   `schema.sql`'s `deduct_inventory_fifo`/`sale_events` is DEAD — the real
   sale path is `record_sale()` writing to a `sales` table, matching
   CLAUDE.md's prose description but NOT its function-name claim — worth
   fixing CLAUDE.md separately, low priority). Relevant body:
   ```sql
   IF p_listing_id IS NOT NULL THEN
       UPDATE platform_listings
       SET quantity_listed = GREATEST(quantity_listed - p_quantity, 0),
           status = CASE WHEN GREATEST(quantity_listed - p_quantity, 0) = 0
                         THEN 'out_of_stock' ELSE status END,
           synced_at = now()
       WHERE platform = COALESCE(p_platform,'ebay')
         AND listing_id = p_listing_id AND external_id = COALESCE(p_external_id,'');
   ```
   **Conflict**: this RPC bumps `synced_at` on every ordinary sale, not
   just on a real price/qty push. If the new `--push` command also stamps
   `synced_at` on success and `--push`'s "pending changes" detection
   compares `synced_at` vs `updated_at`, a plain sale (unrelated to a
   recalc'd price change) would make an unpushed price change look
   already-synced and get silently skipped.
   **Fix applied**: added a NEW column `price_synced_at` (see revised DDL)
   for the push command's own bookkeeping, leaving the existing `synced_at`
   alone for `record_sale`'s use. `--push` compares `updated_at` vs
   `price_synced_at`, not `synced_at`.
   Also: `quantity_listed` is a live "remaining count as far as our DB
   knows" counter, decremented per-sale — NOT eBay's cumulative
   `<Quantity>` semantics (that distinction is still eBay-side only, per
   original Step 0 #6). The push engine should update
   `platform_listings.quantity_listed` to the freshly-pushed
   desired-available count after every successful push, so it stays a
   correct baseline for `record_sale`'s ongoing decrements and for
   `--ebay-reconcile` — the original plan doc didn't call this out
   explicitly under "Sync engine flow" and it's now added there.

4. **The web app already has a pre-existing manual sync-gating value**:
   `platform_listings.status` includes `'do_not_sync'` (alongside `active`,
   `draft`, `delisted`, `out_of_stock`), with a full Inventory-tab UI
   (create/edit listing modals, status dropdown, colored badge) already
   built in `card-board-mastermind-WebInvManagement/inventory.js`. Today
   nothing automated reads this status — it's purely a manual bookkeeping
   label (used for display filtering only) since no push feature has
   existed until now.
   **This was not knowable when the plan was locked** and changes the
   gating implementation (not the "Locked decision" itself — a per-listing
   allowlist remains the design): the new `sync_enabled` boolean is still
   built as planned (the granular rollout dial), but the engine ALSO
   hard-excludes any listing where `status != 'active'` regardless of
   `sync_enabled` — so a listing a user has already marked `do_not_sync`,
   `draft`, or `delisted` in the existing UI is never touched by the new
   engine even if `sync_enabled` were somehow true. This is additive
   safety, not a reinterpretation of the locked allowlist mechanism.

5. **`card_sets.total_cards` likely already covers the plan's proposed
   `official_set_total` column.** Confirmed in
   `card-board-mastermind-WebInvManagement/configuration.js`: the Set
   editor already has a manually-entered "Total cards" field
   (`card_sets.total_cards`) displayed as `count / total_cards` in the
   Configuration UI — the same printed-denominator concept the plan wanted
   for `{set_total}` / `{number:pad}`. **Do not add
   `card_sets.official_set_total`** — reuse `total_cards`, backfill any
   sets missing a value instead of adding a new column. (Separately,
   `set_pricing_config` also has its own `set_total_cards` and
   `common_max_card_num` columns — these feed a THIRD, card-number-range-
   based card-type classification that coexists with `pricing_engine.py`'s
   rarity-string classification. Not needed for v1 of this feature, but
   worth knowing a third classification approach already exists in the
   Configuration UI before extending `card_type_mapping`.)

6. **`record_sale` and `delete_platform_listing` RPCs are called directly
   from the web app** (`sales.js`, `inventory.js`) as well as from CBMM
   (`ebay_orders.py`) — confirms these are shared, live, high-traffic RPCs
   per the CLAUDE.md cross-repo rule. Nothing in this build renames or
   changes their signatures, so no break expected, but any new trigger
   added to `platform_listings` (e.g. for `updated_at`) must not interfere
   with `record_sale`'s existing `UPDATE platform_listings` statement.

7. Confirmed `card_master.card_number_numeric` (integer) already exists —
   use this directly for `priority_rule` card-number sorting rather than
   parsing the text `card_number` column.

8. **⚠ ARCHITECTURAL GAP — surfaced, not silently resolved (real $ stakes,
   needs Fei's confirmation before this pricing pipeline goes live):**
   `card_type_mapping` was designed to classify tier (`common` / `holo` /
   `reverse_holo` / `ultra_rare_rule`) purely from the 7 variant axes. Live
   data proves this is impossible — `foil_type` does NOT separate
   ultra-rares from plain cards at all:
   ```
   foil_type='holo':         Rare, Rare Holo, Illustration Rare, Ultra Rare,
                              Hyper Rare, Special Illustration Rare, Common(!)
   foil_type='non_holo':     Common, Uncommon, Illustration Rare, Ultra Rare,
                              Hyper Rare, Special Illustration Rare
   foil_type='reverse_holo': Common, Uncommon, Rare, ACE SPEC Rare
   ```
   Ultra-rare-ness is carried entirely by `card_master.rarity` (a different
   table), which is NOT one of `card_type_mapping`'s match columns. Seeding
   e.g. `foil_type='holo' -> tier_card_type='holo'` would misclassify
   every Illustration Rare / Ultra Rare / Hyper Rare card with that
   foil_type as plain `holo` tier — i.e., real risk of listing genuinely
   expensive cards at common/holo-tier prices.
   **Resolution applied for now** (safe, reversible, does not block the
   build): kept `card_type_mapping` exactly as designed for its narrower,
   legitimate use — axis-specific overrides (promo `source_type`,
   `stamp_type` special cases) — seeded with ONLY the required all-NULL
   wildcard → `'common'` (satisfies "every card resolves"). Tier
   classification for everything else uses `utils/pricing_engine.py`'s
   existing `card_master.rarity`-based `RARITY_TO_TYPE` map as the primary
   signal (this is the ONLY reliable signal in the live data); a
   `card_type_mapping` row only fires as an override on top of that when
   one specifically matches. **This needs Fei to confirm before this goes
   live for real listings** — it's a change from the plan's literal "7-axis
   only" wording, made necessary by what the data actually looks like, not
   a preference call.

   **RESOLVED by Fei's handoff addendum (2026-07-21).** `card_type_mapping`
   rebuilt (dropped and recreated — it only ever held one throwaway
   wildcard row, no consumers existed yet) with:
   ```sql
   CREATE TABLE card_type_mapping (
     id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     platform text NOT NULL DEFAULT 'ebay',
     account text,
     rarity text,               -- NULL = wildcard on rarity
     variant_key text,          -- NULL = wildcard; non-NULL = LIKE pattern
                                 -- matched against card_variants.variant_key
     tier_card_type text NOT NULL,
     priority integer NOT NULL DEFAULT 10,
     created_at timestamptz DEFAULT now()
   );
   ```
   Resolver (most-specific-wins: both fields non-NULL beats one beats
   wildcard; `priority` only breaks ties within equal specificity;
   `created_at DESC` is the final tie-break):
   ```sql
   SELECT tier_card_type FROM card_type_mapping
   WHERE platform = 'ebay' AND (account = :account OR account IS NULL)
     AND (rarity IS NULL OR rarity = :card_rarity)
     AND (variant_key IS NULL OR :card_variant_key LIKE variant_key)
   ORDER BY (rarity IS NOT NULL)::int + (variant_key IS NOT NULL)::int DESC,
            priority DESC, created_at DESC
   LIMIT 1;
   ```
   Diagnostic run confirmed `card_variants.variant_key` is a pipe-delimited
   concat of the 7 axes in order, e.g. `'non_holo||||||'` /
   `'reverse_holo||||||'` — so `variant_key LIKE '%reverse_holo%'` reliably
   matches any reverse-holo variant regardless of the other 6 axes.
   Live rarity distribution (`card_master.rarity`, 16 distinct values incl.
   NULL): Common 2059, Uncommon 1619, Rare 613, Double Rare 307,
   Illustration Rare 289, Ultra Rare 271, Rare Holo 249, Shiny Rare 107,
   NULL 106, Special Illustration Rare 73, Hyper Rare 43, ACE SPEC Rare 31,
   Promo 25, Shiny Ultra Rare 11, Mega Hyper Rare 1, MEGA_ATTACK_RARE 1
   (this last one's inconsistent SCREAMING_SNAKE_CASE vs. every other
   rarity's Title Case looks like a data-quality issue, not a real rarity
   — flagged for Fei separately, not tier-assigned).
   Seeded exactly 10 rows (all match confirmed real rarity strings, no
   adjustment needed):
   | rarity | variant_key | tier_card_type | priority |
   |---|---|---|---|
   | NULL | NULL | common | 10 |
   | Common | NULL | common | 10 |
   | Uncommon | NULL | common | 10 |
   | Rare | NULL | holo | 10 |
   | Rare | `%reverse_holo%` | reverse_holo | 10 |
   | Rare Holo | NULL | holo | 10 |
   | Ultra Rare | NULL | ultra_rare_rule | 10 |
   | Illustration Rare | NULL | ultra_rare_rule | 10 |
   | Special Illustration Rare | NULL | ultra_rare_rule | 10 |
   | Hyper Rare | NULL | ultra_rare_rule | 10 |
   **Deliberately NOT seeded** (Fei's explicit choice — "I'd like to have
   them added to the config page and I can configure them myself"):
   Double Rare (307 cards — real market premiums, e.g. ex cards), Shiny
   Rare (107), ACE SPEC Rare (31), Promo (25), Shiny Ultra Rare (11), Mega
   Hyper Rare (1), plus NULL/MEGA_ATTACK_RARE (data-quality issues, not
   tier decisions). **New required deliverable**: a `card_type_mapping`
   editor in the web repo's Configuration UI so Fei can add these himself
   (see "Files expected to change"). Until built, these rarities resolve
   via the wildcard → `common` — **the recalc engine's `--dry-run` output
   MUST flag every card whose resolution came from a specificity-0
   (wildcard) row**, so a new set's unmapped rarity doesn't silently
   under-price. This is a hard requirement, not a nice-to-have.

9. **BLOCKING - `listing_templates` already exists as a live, real,
   actively-maintained feature with a completely different schema than
   this plan assumed. Migration paused on this table specifically pending
   Fei's decision.**
   `CREATE TABLE IF NOT EXISTS listing_templates (...)` in migration 001
   silently no-op'd against a table that already existed - it's in
   `schema.sql` (comment: "Defines eBay listing structure (commons vs
   reverse holo/ultra rare)"), was actually run against the live DB on
   2026-05-31 (real `created_at` timestamps), has 2 real seeded rows tied
   to a real account (`BIGGYFISH`):
   - `'commons'`: `included_types=['common','uncommon','trainer','holo',
     'double_rare']`, `excluded_types=['reverse_holo']`, free shipping.
   - `'reverse_holo_ultra'`: `included_types=['reverse_holo','holo',
     'double_rare','illustration_rare','special_illustration_rare',
     'ultra_rare','hyper_rare']`, $0.79 + $0.10/card shipping.
   ...and has a full live Configuration UI in
   `card-board-mastermind-WebInvManagement/configuration.js` (list view,
   create/edit modal, delete) that Fei actively uses today. This is not
   dead/unused schema - it's a real, in-use feature.
   Its actual columns: `id, platform, name, description, included_types
   text[], excluded_types text[], card_num_min, card_num_max,
   shipping_base, shipping_per_card, max_quantity, created_at, account`.
   None of the plan's new template columns (`base_price`,
   `default_quantity_limit`, `low_stock_threshold`, `low_stock_bump`,
   `listing_kind`, `priority_rule`, `display_sort`, `name_format`,
   `card_type_filter`, `updated_at`) exist on it, and the CREATE TABLE
   statement did not add them (table already existed).
   **Why this matters beyond a naming clash**: the two existing rows
   ('commons' / 'reverse_holo_ultra') are already the same two archetypal
   templates the plan's Rollout sequence step 1 wanted to create
   ("RH-to-UR" and "base-commons") - this is almost certainly the same
   underlying feature, built in an earlier session, that this plan didn't
   get reconciled against before being locked. Its `included_types` is a
   rarity-string array (not the 7-axis `card_type_mapping` this plan
   designed) - which lines up with finding #8: rarity-string is the real
   classification signal already in use elsewhere in this system.
   **Action taken**: did NOT alter or add rows to this table. The FK
   `platform_listings.template_id -> listing_templates(id)` from migration
   001 did apply (harmless, nullable, unpopulated), but nothing depends on
   it yet. Paused: seeding new listing_templates rows, and any recalc/push
   logic that reads template-level qty defaults / low-stock bump / naming
   config / listing_kind.
   **This is a genuine decision for Fei, not something to guess**: most
   likely resolution is extending the existing `listing_templates` table
   additively with the new columns (keeping `included_types` /
   `excluded_types` / `card_num_min` / `card_num_max` / `shipping_base` /
   `shipping_per_card` / `max_quantity` exactly as-is - they're live and
   used by the Configuration UI) and mapping the plan's new membership
   concept (`card_type_filter`) onto the existing `included_types` /
   `excluded_types` rather than adding a redundant third field. But this
   changes both a DB table with real business data on it and a live
   Configuration UI screen - flagging for explicit confirmation rather
   than doing it silently.

   **RESOLVED by Fei's handoff addendum (2026-07-21): extend the existing
   table, don't rename/replace.** Dumped full schema + both rows verbatim
   before altering (exact values above are what was live). Applied:
   ```sql
   ALTER TABLE listing_templates ADD COLUMN base_price numeric;
   ALTER TABLE listing_templates ADD COLUMN default_quantity_limit integer;
   ALTER TABLE listing_templates ADD COLUMN low_stock_threshold integer DEFAULT 8;
   ALTER TABLE listing_templates ADD COLUMN low_stock_bump integer DEFAULT 1;
   ALTER TABLE listing_templates ADD COLUMN listing_kind text NOT NULL DEFAULT 'variation';
   ALTER TABLE listing_templates ADD COLUMN priority_rule text DEFAULT 'card_number';
   ALTER TABLE listing_templates ADD COLUMN display_sort text DEFAULT 'card_number';
   ALTER TABLE listing_templates ADD COLUMN name_format text DEFAULT '{number}/{set_total} {name} {suffix}';
   ALTER TABLE listing_templates ADD COLUMN card_type_filter text[];
   ALTER TABLE listing_templates ADD COLUMN updated_at timestamptz DEFAULT now();
   -- platform and account already existed on the live table — not re-added.
   ```
   Backfilled the two existing rows:
   - `commons`: `base_price=0.99` (**inferred, not given by Fei — flagged
     for confirmation**: the `common` card_type's own cheapest `price_tiers`
     rung for account `BIGGYFISH` is exactly $0.99, i.e. the floor already
     implied by existing tier config), `priority_rule='card_number'`,
     `display_sort='card_number'`.
   - `reverse_holo_ultra`: `base_price=1.37` (per the addendum's given
     value; cross-checked against live `price_tiers` — account
     `BIGGYFISH`'s `reverse_holo` AND `common` tiers both have their
     *second* rung at exactly $1.37, so this is consistent with existing
     pricing config, not an arbitrary number), `priority_rule=
     'rh_then_number_holo_last'`, `display_sort='card_number'` (per Step 0
     #7 resolution below — interleaved, no band support needed).
   `card_type_filter` left NULL on both rows — deliberately not
   auto-derived from `included_types`/`excluded_types`. Reason: those
   arrays mix concepts that don't map 1:1 onto the new `tier_card_type`
   domain (`'trainer'` isn't a pricing tier at all; `'double_rare'` is one
   of the rarities Fei chose to defer to manual `card_type_mapping`
   configuration, not something to auto-decide here). Left NULL (=
   no filter) rather than encode a guessed, lossy translation; existing
   `included_types`/`excluded_types`/`card_num_min/max`/`shipping_base`/
   `shipping_per_card`/`max_quantity` are untouched and still drive the
   live Configuration UI exactly as before.
   **Step 0 #7 also resolved**: Fei confirmed the dropdown is a plain
   single-key sort ("listed depending on sort ordering — alphabet, or
   numeric, or by set release date" for future cross-set Pokémon-themed
   listings) — no RH-blocked-first band grouping needed. `display_sort`
   domain is now `'card_number' | 'alpha' | 'release_date'` (the third
   value reserved for the deferred themed-listings feature).

## Step 0 — prep findings so far (2026-07-21, pre-build, no DB access yet)
- 7-axis column names in `card_variants` confirmed exactly as assumed above:
  `foil_type`, `foil_pattern`, `texture`, `material`, `size`, `stamp_type`,
  `source_type`. No changes needed to the DDL sketches.
- `card_sets` PK is `id`; join path is `card_master.set_id = card_sets.id`.
- `market_prices` joins via `variant_id` (+ `condition`), not `card_id`.
- `ebay_listing_map(item_id, variation_name) -> variant_id, condition` is
  used throughout `importer/ebay_orders.py` / `ebay_picking.py` but **is not
  defined in `schema.sql` at all** — confirms it's live-DB-only schema
  drift (per the CLAUDE.md warning). Do not trust `schema.sql` for this
  table; a live DB introspection query is required before writing the
  `listing_card_assignments` migration.
- `schema.sql`'s `platform_listings` definition (cols: id, inventory_id,
  platform, external_id, list_price, status, listed_at, synced_at) is
  almost certainly stale too — code references `quantity_listed` and an
  `account` split that aren't in that DDL. Needs the same live check before
  adding `template_id` / `base_price` / `sync_enabled` / `synced_at`
  (synced_at may already exist!) / confirming `updated_at` presence.
- Step 0 #5 (reusable pricing code) — found: `utils/pricing_engine.py`
  already implements a 4-layer hierarchy (card override → set config
  multiplier+floor → price_tiers → platform default) against
  `card_pricing_overrides`, `set_pricing_config`, `price_tiers` (these
  three ARE defined in `schema.sql` and look current). **Reconciliation
  needed**: its card-type classification (`get_card_type()`) maps from the
  PokemonTCG **rarity string** via a `RARITY_TO_TYPE` dict — it does NOT
  use the 7-axis `card_variants` model at all. The new pipeline's step 2
  (`card_type_mapping`, 7-axis based) is a different classification
  mechanism than what's live today. Decide during build whether
  `card_type_mapping` replaces `RARITY_TO_TYPE` entirely or the two need to
  agree/merge — this weakens the "reuse, don't reimplement" assumption in
  Step 0 #5 into "reuse the tier/override/floor plumbing, replace the
  classification layer."
- No `migrations/` folder exists in the repo — all schema changes historically
  went straight into Supabase's SQL editor per CLAUDE.md. The upcoming DDL
  will need to be run the same way (no migration tooling to wire up).

## Rollout sequence
1. Migrations + seed `card_type_mapping` + create the RH-to-UR and
   base-commons templates.
2. Backfill `listing_card_assignments` for 1-2 test listings only:
   walk the live `<Variations>` from `GetItem`, resolve each variation
   to its variant_id via `ebay_listing_map`, insert one assignment row
   per with `status='active'` and `priority_rank` computed via the
   template's `priority_rule`. Unmatched variations → skip + report,
   don't guess.
3. Flip `sync_enabled=true` on 1-2 test listings via the Configuration UI.
4. `--recalc --dry-run` → review → `--recalc` live → eyeball prices in UI.
5. `--push --dry-run` → review → `--push` live on ONE listing →
   spot-check on eBay (prices, quantities, variation count).
6. Exercise one 250-cap promotion deliberately when a real sell-out occurs.
7. Widen listing allowlist gradually (individual toggles or bulk-enable-
   in-set once trust builds for a whole set).

## Future (explicitly deferred, slots reserved)
- **Themed/Pokémon listings** (all cards of one Pokémon, evolution lines) —
  cross-set membership. `listing_card_assignments` already supports this
  (membership is per-variant); ⚠ DO NOT add a set_id shortcut to the
  assignments table during build. New priority_rule values (release_date,
  evolution_stage) + a "populate assignments from a rule" feature will come
  with it (evolution data: pokemontcg.io `evolvesFrom`).
- **New themed eBay accounts** (per-Pokémon-type, mostly single-card
  listings) — covered by account scoping on every table + `listing_kind =
  'single'` engine path. Each new account needs BOTH token flows (OAuth
  consent AND Auth'n'Auth) per the runbook gotcha.
- **`--set` CLI scope** — bulk-target all enabled listings whose cards are
  in a set. Deferred; add if per-listing/per-card targeting proves too
  tedious. Trivial to add later (query filter, no schema).
- Desirability rating (per-card, platform-scoped, scales price AND qty) —
  pipeline step 5 + qty formula hook reserved. Needed before mass listings
  go live from the DB.
- Trigger-on-sale sync (call site = the per-card sync function).
- Scheduling.
- Formula-path tier bumps (if rotation pricing for UR cards is ever wanted
  beyond price_multiplier).
- New listing creation (templates will grow title/description/image rules
  then, not before).

## Files expected to change (build session)
- ✅ New: `importer/ebay_variations_xml.py` (shared XML helper, extracted
  from `rename_variation.py`, verified working)
- ✅ New: `importer/ebay_listing_sync.py` (recalc + push, both built)
- ✅ `main.py` — `--ebay-recalc-prices` / `--ebay-push-listings` +
  `--item-id` / `--card` / `--allow-decreases` / `--force` scope flags
- ✅ Migrations applied: 3 new tables (`card_type_mapping`,
  `platform_sync_status`, `listing_card_assignments`) + 1 extended
  (`listing_templates`, in place — see finding #9); `platform_listings`
  gained `template_id`, `base_price`, `sync_enabled`, `price_synced_at`,
  `updated_at` (+trigger); `set_pricing_config.tier_bump`;
  `card_pricing_overrides.quantity_limit`. `card_sets.official_set_total`
  was NOT added (reuses existing `total_cards` — finding #5).
- ⏳ Configuration UI (web repo, not started this session):
  - **New required item**: `card_type_mapping` editor (rarity +
    variant_key → tier_card_type) — Fei explicitly wants to self-serve the
    unmapped rarities (Double Rare, Shiny Rare, ACE SPEC Rare, Promo,
    Shiny Ultra Rare, Mega Hyper Rare) through this rather than a hardcoded
    seed (finding #8 resolution).
  - Extend the existing `listing_templates` screen
    (`configuration.js`) with the 9 new columns — do NOT build a new
    screen, the table/UI already exists (finding #9 resolution).
  - Per-listing sync toggle + bulk-enable-in-set action (enumerates a
    set's listings via `platform_listings` ↔ variant ↔ card ↔ `card_sets`
    — no set-level table needed), platform/account kill-switch controls,
    template dropdown on listings.
