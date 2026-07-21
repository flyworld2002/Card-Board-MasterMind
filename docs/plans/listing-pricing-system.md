# Plan: Listing Pricing System (profiles + rules + pins)

## Status (2026-07-21)
Full replacement of the `card_type_mapping` + `price_tiers`-as-global +
`set_pricing_config` multiplier/floor pipeline built in
`docs/plans/ebay-listing-sync.md` — confirmed explicitly with Fei (see that
doc's own architecture is now superseded for pricing; `sync_enabled` /
`platform_sync_status` / `listing_templates` / `listing_card_assignments`
from that build are UNAFFECTED and still apply — this only replaces how a
*price* is resolved, not the sync-gating or 250-cap machinery).

Original spec from Fei, verified against live schema, corrections below.

## Corrections made to the original spec (Step 0 audit)
1. **`platform_listing_lines` does not exist.** There is no parent-listing/
   child-line split — `platform_listings` is flat, one row per variant per
   listing (confirmed same table used throughout `ebay-listing-sync.md`).
   All the spec's new `platform_listing_lines` columns (`manual_price`,
   `low_stock_qty`, `pushed_price`, `pushed_qty`, `pushed_at`) go directly
   on `platform_listings` instead.
2. **Every PK in this schema is `uuid`**, not `bigint generated always as
   identity` (confirmed: `card_master.id`, `card_sets.id`,
   `card_variants.id`, `platform_listings.id` are all uuid). New tables
   (`pricing_profiles`, `pricing_profile_tiers`, `listing_pricing_rules`)
   use `uuid default gen_random_uuid()` to match. FK columns
   (`match_set_id`, `match_card_id`) are `uuid`, not `text`/`bigint`.
3. **What "listing" means for a rule, given the flat schema**: one eBay
   multi-variation listing (`platform_listings.listing_id`, the eBay
   ItemID) spans many `platform_listings` rows — confirmed live, up to
   **244 rows for one `listing_id`**. A `listing_pricing_rules` row must
   therefore key off `(platform, listing_id)` — the shared eBay-item
   identifier — NOT a FK to one specific `platform_listings.id` (which
   would only ever match one variant, defeating the point of a label-level
   rule). `listing_pricing_rules.platform_listing_id_text` (naming TBD)
   stores that shared identifier; matching still happens per-row against
   each `platform_listings` row that shares it.
4. `foil_types.code` is `text` — `match_foil_type text references
   foil_types(code)` in the original spec is correct as written.
5. No `inventory_available` table/view exists — `available_qty` is
   computed the same way `_compute_desired_qty` already does in
   `importer/ebay_listing_sync.py` (SUM(quantity - quantity_sold) from
   `inventory`, scoped by variant_id + condition via `ebay_listing_map`).
   Reused, not reinvented.

## Concepts (unchanged from spec)
- **Pricing profile**: named, reusable tier table. Knows nothing about
  listings/platforms. e.g. `double_rare_common` (< $1 → 4.99, ≥ $1 → 5.99),
  `double_rare_rh_ur` (< $1 → 3.99, ≥ $1 → 4.99).
- **Listing rule**: lives on a listing (see correction #3 — keyed by
  shared eBay item id, not a single row's PK). Maps cards matching
  criteria (rarity / foil_type / set / card, all nullable = wildcard) to a
  profile. Most-specific-match wins (count of non-null match columns;
  ties broken by `priority` ascending, then newest rule).
- **Pin**: `manual_price` on a `platform_listings` row. Sync never
  overwrites it. Clearing it returns the row to rule-based pricing.
- **Single listing**: one rule (profile-driven) or no rules + a pin.

## Resolution order (per platform_listings row, at sync time)
```
1. row.manual_price            -- pinned; sync must never overwrite
2. most specific listing rule  -- match card attrs, use rule's profile tiers
3. platform default price      -- failsafe; UI flags rows that fall here
```
No global pricing layer outside the listing — `set_pricing_config`'s
multiplier/floor is retired. Set-specific treatment becomes a set-scoped
rule (`match_set_id`) or a pin.

## Schema (corrected DDL — see docs/plans/listing_pricing_migration_001.sql)
- `pricing_profiles(id uuid pk, name text unique, notes, default_low_stock_qty, created_at)`
- `pricing_profile_tiers(id uuid pk, profile_id uuid fk, min_market numeric inclusive, max_market numeric exclusive/null=open-ended, list_price numeric)`
- `listing_pricing_rules(id uuid pk, platform text, listing_id text [the shared eBay item id], profile_id uuid fk, match_rarity text, match_foil_type text fk foil_types(code), match_set_id uuid fk card_sets(id), match_card_id uuid fk card_master(id), priority int default 100, low_stock_qty int nullable)`
- `platform_listings` gains: `manual_price numeric`, `low_stock_qty integer`, `pushed_price numeric`, `pushed_qty integer`, `pushed_at timestamptz`

## RPC: `resolve_listing_prices(p_platform text, p_listing_id text)`
Postgres function (style of `push_staging_row_to_inventory.sql`), one row
per `platform_listings` row sharing that `(platform, listing_id)`:
```
row_id, card_id, variant_id, derived_label, market_price, resolved_price,
price_source ('pin' | 'rule:<rule_id>' | 'default'), available_qty,
low_stock_qty (row override -> rule -> profile default)
```
Called by both the web grid (on page load) and the CLI push job, so web
and CLI can never disagree — resolution lives in Postgres, not JS or
Python.

## Label derivation
`label = card_master.rarity [+ ' ' + foil_type display when set]` —
derived from structured data, never from eBay's freeform variation text.
eBay variation names stay display-only strings on `ebay_listing_map`.

## Sync flow (three layers)
- **Stored**: profiles + tiers, listing rules, `manual_price`,
  `low_stock_qty`, `pushed_*` snapshot. Resolved price is NEVER stored as
  a source of truth — always computed fresh by `resolve_listing_prices()`.
- **Computed**: `resolve_listing_prices()` on demand.
- **Pushed**: explicit step, Python side (eBay creds stay in the CLI's
  `.env`, browser never talks to eBay directly):
  1. Web page → user reviews grid, adjusts pins/qty.
  2. Push button POSTs to FastAPI (`picking_api.py`, new `/push-prices`
     endpoint, same `PICKING_API_TOKEN` auth as the existing picking flow).
  3. Triggers `--ebay-pushprices --listing-id <ID> --account N` (new
     `importer/` module wired to `main.py`): calls
     `resolve_listing_prices()` → computes qty to send (gated by
     low_stock_qty, floored at 0 — see Open Question 2, recommend GATE) →
     diffs against `pushed_price`/`pushed_qty`, sends ONLY changed
     variations (a 244-variation listing must not re-send 244 updates for
     3 changes) → revises via Trading API → writes `pushed_price`/
     `pushed_qty`/`pushed_at` back per row.
  4. Web re-reads, shows in-sync vs. "computed $X, live $Y — needs push".
  Pinned rows are read-only inputs to the pusher. Supports `--dry-run`
  (prints the would-be diff) per repo convention.

## UI (card-board-mastermind-WebInvManagement)
- Configuration → **Pricing profiles** tab (alongside Sets/Card
  games/Listing templates): CRUD for profiles + tier rows, mirroring the
  generic `ATTR_TABLES` pattern where reasonable.
- New **Listing pricing** page module (`renderX(container)` convention):
  header (listing name/platform/line count) → rows grouped by derived
  label → group header shows assigned profile + tiers inline + a profile
  picker (assigning one creates/updates the rule for that listing) → "no
  rule" warning banner per unmatched label group → row detail
  (name/number, market price, resolved price, source badge, available
  qty) → editing price writes `manual_price` (pin, visually distinct);
  clearing nulls it → per-row sync status (`pushed_*` vs resolved, diff
  shown) → Push button → `/push-prices` → Advanced: scoped rule add
  (set-/card-scoped) with a specificity note.

## Migration notes (from the original pricing_engine.py + this session's build)
- card manual override → pin (`manual_price`)
- set multiplier/floor → retired; recreate only where actually needed as
  a set-scoped rule
- global price-tier table (`price_tiers`) → seed the first profiles from
  it, not consumed going forward
- `card_type_mapping` (built + seeded last session, see
  `ebay-listing-sync.md` finding #8) → superseded by `match_rarity` /
  `match_foil_type` directly on `listing_pricing_rules`. NOT dropped (real
  seeded data, and the web Configuration UI editor for it already
  shipped) — just no longer consulted by the new resolution path. Leave
  in place; revisit whether to remove later once this system is trusted.
- platform default → unchanged failsafe
- Seed: `double_rare_common` / `double_rare_rh_ur` profiles with the
  4.99/5.99 and 3.99/4.99 tiers, attached via rules to the `commons` and
  `reverse_holo_ultra` listings' live eBay item IDs (need Fei to confirm
  which real `listing_id` values these are — the templates aren't 1:1
  with a single eBay item, `commons`/`reverse_holo_ultra` are
  *templates* shared across potentially many listings; the rule attaches
  to one specific live listing_id, not the template).

## Build progress (2026-07-21)
- ✅ Migration 001 applied: `pricing_profiles`, `pricing_profile_tiers`
  (with a working non-overlap trigger — verified it actually rejects a
  conflicting tier insert, not just present), `listing_pricing_rules`,
  and the 5 new `platform_listings` columns.
- ✅ `double_rare_common` / `double_rare_rh_ur` profiles + tiers seeded
  (4.99/5.99 and 3.99/4.99 exactly per Fei's example).
- ✅ `resolve_listing_prices(platform, listing_id)` RPC built and verified
  against a real live listing (`335662210469`, 244 rows) for all three
  resolution paths: `default` (no rules yet — market×2+1 formula, e.g.
  $0.79 → $2.58), `rule:<id>` (temporary test rule, rolled back after —
  Double Rare under $1 → $4.99, over $1 → $5.99, matching spec exactly),
  and `pin` (manual_price overrides everything, rolled back after). No
  real data was left behind by any of these tests.
- ✅ `--ebay-pushprices --listing-id <ID> [--account N] [--dry-run] [--quiet]`
  built (`importer/ebay_pushprices.py`), wired to `main.py`. Diffs
  `resolve_listing_prices()` output against `pushed_price`/`pushed_qty`,
  sends ONLY changed variations (verified: 244/244 rows flagged as
  changes on a never-pushed listing; simulating a full push then re-diffing
  correctly found 0 changes — the "only push what changed" logic is
  solid). Handles both the multi-variation path and the single-listing
  path (no `<Variations>` block). low_stock_qty gates pushed quantity
  (`available - low_stock_qty`, floored at 0) per Open Question 2's
  recommendation.
  **Two real bugs caught while testing against live data** (both were
  latent in code from *last* session too, just never exercised with a
  non-empty uuid list before): psycopg2 sends a Python list as `text[]`,
  which doesn't compare against a `uuid` column without an explicit
  `::uuid[]` cast — hit this building `_compute_changes`, then found and
  fixed the identical latent bug in two spots in `ebay_listing_sync.py`
  (`template_ids` / `platform_listing_ids` lookups) that had just never
  been triggered because those lists were always empty in prior testing.
- ✅ `push_prices()` returns a structured summary dict (`{listing_id,
  resolved, changed, pushed, warnings, dry_run}`), matching the existing
  `pull_picking()` convention, instead of only printing — needed so the
  new API endpoint can relay something useful to the web UI.
- ✅ `POST /api/push-prices` added to `picking_api.py` (same
  `PICKING_API_TOKEN` auth, separate lock from the picking pull so the
  two features don't block each other). Verified it imports and registers
  correctly.
- ✅ **Web UI built** (`card-board-mastermind-WebInvManagement`):
  - Configuration → "Pricing profiles" tab (`configuration.js`): CRUD for
    profiles, with tier management nested in a per-profile modal (add/
    edit/delete tier rows; the non-overlap trigger's error message
    surfaces directly in the form).
  - New "Listing pricing" page (`listing-pricing.js`, wired into
    `index.html`'s router + sidebar nav): load one listing by eBay Item #,
    rows grouped by derived label, profile picker per group (assigns/
    updates a `listing_pricing_rules` row), per-row manual price pin and
    low-stock qty editing, sync-status highlighting, Push/Dry-run buttons
    calling the new `/api/push-prices` endpoint on `picking_api.py` (same
    token auth, same LAN-IP convention as `picking.js`).
- ✅ **Caught and fixed a real security gap spanning BOTH build sessions**:
  none of the 6 new tables (`card_type_mapping`, `platform_sync_status`,
  `listing_card_assignments` from last session; `pricing_profiles`,
  `pricing_profile_tiers`, `listing_pricing_rules` from this one) had Row
  Level Security enabled — confirmed via `pg_class.relrowsecurity` —
  while established tables like `listing_templates`/`platform_listings`
  do, with an `"authenticated only"` policy (`auth.role() = 'authenticated'`,
  `FOR ALL`). Table-level grants for `anon`/`authenticated` were already
  identical across old and new tables (default privileges apply
  automatically to new tables), so the new tables weren't *inaccessible* —
  they were actually **more open** than intended, since RLS-off + existing
  grants means no row-level gate at all (technically reachable by the
  `anon` role too, not just signed-in users). Enabled RLS + added the
  identical policy on all 6 tables; re-verified `resolve_listing_prices()`
  still returns correct results afterward (still 244 rows for the test
  listing).
- ⏳ Bugs caught along the way this session (see above): psycopg2
  `uuid[]` cast, `numeric` has no `'infinity'` literal, needsPush() qty
  comparison not accounting for low-stock gating.
- **Not yet done**: attaching the seeded `double_rare_common`/
  `double_rare_rh_ur` profiles to real listings via actual rules (Open
  Question 4 — needs Fei to pick real listing_ids), and no live
  browser/eBay-API test of the new page or push flow (no JS runtime or
  browser available in this environment — verified via brace/paren
  balance checks and manual re-read only, same limitation as last
  session's UI work).

## Open questions (from the spec, plus one found during audit)
1. ~~Exact live names/PKs~~ — RESOLVED above (corrections #1-#5).
2. Should `low_stock_qty` gate pushed quantity (`available - low_stock_qty`,
   floored at 0) or only warn in the UI? Spec recommends gate — proceeding
   with gate unless told otherwise.
3. Tier boundary: min-inclusive / max-exclusive — confirmed by the spec's
   own example ("under $1 → 4.99, $1 and over → 5.99"). Implemented that way.
4. **NEW**: which real `platform_listings.listing_id` values are the
   "commons" and "reverse_holo_ultra" listings for the double-rare seed
   data? A `listing_templates` row can be shared across multiple physical
   eBay listings, so "the commons listing" isn't a single, unambiguous
   `listing_id` — need this confirmed before seeding real rules (seeding
   the profiles + tiers themselves has no such ambiguity and can proceed).
