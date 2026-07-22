# Plan: Listing Pricing System (profiles + rules + pins)

## PIVOT (2026-07-21, later same day): template-centric roster + manual groups
Fei changed the design after using the first version. New shape,
confirmed explicitly:

1. **`listing_templates` becomes ~1:1 with a real listing** — gets a
   `listing_id` column (unique per platform+listing_id). "Template" and
   "listing" are now effectively the same entity; template = that
   listing's header/config, not a shared archetype used by many listings.
2. **The card roster is explicit, not inferred from sync.** Previously
   `resolve_listing_prices()` just read whatever `platform_listings` rows
   already existed (from eBay import). Now `listing_card_assignments`
   (built last session for the 250-cap design, never used) becomes the
   actual roster — a card belongs to a listing by having a row here
   (`status='active'` = live, has a `platform_listings` row via
   `platform_listing_id`; `status='queued'` = planned, not live yet, no
   `platform_listings` row needed).
3. **Grouping is manual, not auto-derived from rarity/foil_type.** New
   `listing_card_groups` table: freely-named, created inline on the
   listing page, cards assigned to a group via `listing_card_assignments.
   group_id`. A group carries a `profile_id` directly — that IS the
   pricing rule now.
4. **`listing_pricing_rules` (built this session) is retired** —
   rarity/foil_type/set/card attribute-matching is replaced entirely by
   explicit manual group membership. Table had 0 rows, dropped cleanly.
5. **New "add card to listing" feature**, supporting queued (not-yet-live)
   cards — you can plan a listing's roster before the cards are live on
   eBay.
6. **`--ebay-push-listings` (session 1) is merged into `--ebay-pushprices`.**
   Both would otherwise walk the same `listing_card_assignments` roster
   for 250-cap promotion — one push command going forward.
   `--ebay-push-listings` / `push()` / `_push_single` /
   `_push_variation_listing` in `ebay_listing_sync.py` are removed.
   `--ebay-recalc-prices` is left alone for now (separate, already-noted
   superseded pipeline, not explicitly asked to be removed this round).

Resolution becomes: card → its group (`listing_card_assignments.group_id`)
→ group's `profile_id` → `pricing_profile_tiers`. `manual_price` pin
(still only meaningful for `status='active'` rows, since that's where
`platform_listings` — and thus the pin column — exists) still overrides
everything, same as before.

### Build log for the pivot (2026-07-21)
- ✅ Migration 003: `listing_templates.listing_id` (unique per
  platform+listing_id), new `listing_card_groups` table, `template_id`/
  `group_id` on `listing_card_assignments`, `listing_pricing_rules`
  dropped (0 rows). **Caught during testing**: the old
  `chk_lca_has_listing_ref` check constraint (platform_listing_id OR
  ebay_item_id) rejected a queued row using only `template_id` — fixed by
  dropping that constraint, making `template_id` `NOT NULL` (now the sole
  listing reference), and dropping the now-redundant `ebay_item_id`
  column. Table was still empty, safe to tighten.
- ✅ Migration 004: `resolve_listing_prices()` rewritten to resolve off
  the roster (`listing_card_assignments` joined to `listing_card_groups`)
  instead of `platform_listings` + the now-dropped `listing_pricing_rules`.
  `row_id` is now `listing_card_assignments.id`; `platform_listing_id` is
  returned separately (NULL for queued rows). Verified end-to-end with a
  rollback-only test: 2 active + 1 queued roster row, all three correctly
  resolved via the assigned group's profile tiers.
- ✅ `ebay_pushprices.py` rewritten: gates only `status='active'` rows
  (queued rows are preview-only, never pushed directly), absorbed the
  250-cap promotion logic from the now-removed `--ebay-push-listings`
  (delete sold-out variation, promote highest-priority queued row,
  **create a new `platform_listings` row for it** — the original
  session-1 implementation never actually did this, a gap fixed here).
  `--ebay-push-listings` / `push()` / `_push_single` /
  `_push_variation_listing` / `_compute_desired_qty` / `_get_listing_kind`
  removed from `ebay_listing_sync.py` entirely (superseded helpers used
  only by them); `_render_variation_name` / `_compute_insert_position` /
  `platform_sync_allowed` kept (still imported by `ebay_pushprices.py`).
  Verified template resolution + roster diffing against real data
  (rollback-only).
- ✅ Web UI rewritten (`listing-pricing.js`): a template IS the listing
  now — page offers to create one if none exists for the typed Item #.
  Manual groups (create inline, rename, delete, assign a profile).
  Checkbox-select + bulk "assign to group." **New "Import into roster"
  action** — surfaces existing `platform_listings` rows for a listing_id
  that predate this system (e.g. Fei's manually-imported 84-row test
  listing) so they don't have to be re-added one by one. **New "Add card
  to listing"** — search catalog by name, pick a variant, adds as
  `status='queued'`. `configuration.js`'s template modal + table gained a
  `listing_id` field.
  **Caught and fixed while writing this**: initially used Supabase's
  embedded-resource join syntax (`card_sets(name)`) in the card search —
  nothing else in this codebase uses that pattern, and the FK relationship
  might not be registered in PostgREST's schema cache; switched to the
  established flat-query + JS-side-map convention used everywhere else in
  this app instead of risking an untested code path.
- Not yet done: no live browser/eBay test (same limitation as every prior
  UI pass — no JS runtime available in this environment).

### Three follow-up requests (2026-07-22)
1. **`low_stock_bump` needed decimal support** — was `integer` (a dollar
   amount), fixed to `numeric(10,2)`. Confirmed via
   `information_schema.columns` before and after. No Python change needed
   (`ebay_listing_sync.py` already did `float(...)`).
2. **Groups need to be "universal"** — clarified with Fei: NOT one shared
   group/profile across listings (that would undo the whole "same card,
   different price per listing" point of this system) — just reusable,
   consistent NAMING. Schema already supported this (`UNIQUE
   (template_id, name)` only prevents duplicate names *within* one
   listing). Added a proper "New group" modal with a `<datalist>` of every
   group name used anywhere, replacing the old bare `window.prompt()` —
   picking a suggested name still creates a separate, listing-scoped row
   with its own profile assignment.
3. **Listing templates moved entirely into the Listing pricing page** —
   removed from Configuration (nav item, section, state, all functions)
   per Fei's choice to move rather than duplicate. `index.html`'s router
   and `configKeys` array updated to drop the `listing-templates` route.
   The Listing pricing page's landing view is now a template list (ported
   the create/edit modal from `configuration.js`, minus the
   `included_types`/`excluded_types`/`card_num_min/max`/`shipping_*`/
   `max_quantity`/`priority_rule`/`card_type_filter` fields — those are
   `listing_templates` columns tied to the OLD retired pricing pipeline
   and the old `listing_kind`/priority-based promotion queue ordering,
   not used by the new roster+groups model; can be re-added to this modal
   later if a real need for them resurfaces). Clicking a template row
   opens its roster/groups view (the existing post-Load flow), with a new
   "← Back to templates" button to return.

### Four more follow-ups (2026-07-22)
1. Migration 005: `resolve_listing_prices()` now also returns `set_name`
   and `card_number_numeric`, with `ORDER BY set_name, card_number_numeric`
   at the SQL level. Verified read-only against real listings the user
   had already created between sessions. **No JS change needed** — the
   grouping loop in `listing-pricing.js` just splits `state.resolvedRows`
   into buckets in iteration order, so DB-sorted input stays sorted in
   every bucket (grouped and ungrouped) for free.
2. "New group"'s naming suggestions switched from a native `<datalist>`
   (only reliably shows on typing, not focus, across browsers) to a
   hand-rolled dropdown that shows all names on focus and filters as you
   type — `mousedown` (not `click`) on a suggestion so it registers before
   the input's `blur` hides the dropdown.
3. Added a "Groups" tab to Configuration (`listing_card_groups` — rename,
   reassign profile, delete without opening the listing; day-to-day
   creation stays inline on the Listing pricing page since a group needs
   a template/listing context). **Caught while building it**: initially
   referenced `profilesState.profiles`, which is only populated if the
   user already visited the Pricing profiles tab this session — fixed to
   load profiles independently inside `loadGroups()`.
4. Shift-click range-select added to the roster's row checkboxes — click
   (not `change`, to read `e.shiftKey`) toggles every checkbox between the
   last-clicked one and the current one to match the just-clicked state,
   using the checkboxes' DOM order (spans group boundaries) as the range.

### Three more fixes (2026-07-22) — two were real, confirmed-live gaps
1. **`listing_templates.base_price` (floor) was never respected** —
   confirmed by grepping the RPC and push code for it, zero hits. Fixed
   in migration 006: applied as `GREATEST(computed_price, base_price)`,
   skipped for pins (an explicit human price shouldn't be second-guessed
   by a safety-net floor). Verified against real data — the floor
   correctly didn't change a price that already exceeded it (math
   confirmed: market $0.22 → formula $1.44 → floor $0.99 → stays $1.44).
2. **`listing_templates.default_quantity_limit` was never respected**
   either — `ebay_pushprices.py` only ever used `low_stock_qty` for
   gating, never capped by any quantity limit. Migration 006 added a
   resolved `quantity_limit` output column. **Caught while verifying**:
   my first attempt had the precedence backwards
   (`COALESCE(row_quantity_limit, template_default, 24)`) — confirmed via
   query that ALL 9,363 `platform_listings` rows have
   `quantity_limit=18`, i.e. purely the column's blanket default from an
   earlier migration, never a real per-card override, so it was silently
   always winning over the template's actual configured value. Flipped to
   `COALESCE(template_default, row_quantity_limit, 24)`. Verified: a row
   with `available_qty=34` now correctly caps `qty_to_push` at the
   template's `default_quantity_limit=24`. Applied in both
   `_compute_roster_changes` and `_do_promotions` (250-cap promotion).
   Also made the profile-tier lookup fall back to the platform-default
   formula instead of returning NULL when a profile has no matching tier
   — more likely now that profiles can be created with zero tiers
   momentarily via the new inline-creation flow (item 3 below).
3. **Inline profile creation on the Listing pricing page** — a group's
   profile `<select>` gained a "+ New profile..." option opening a modal
   (name, optional default low-stock qty, one or more tier rows, "+ Add
   tier"), which creates the profile, its tiers, and assigns it to the
   group in one step. Generates the profile's id client-side via
   `crypto.randomUUID()` rather than reading it back after insert with
   `.select().single()` — that chaining pattern isn't used anywhere else
   in this codebase, so avoided it in favor of the plain
   insert-with-a-known-id shape already used throughout.

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

## Post-build self-review finding + fix (2026-07-21, later same day)
Asked to check my own work. Found a real, significant gap:
**`resolve_listing_prices()` had zero awareness of `sync_enabled` /
`status` / `platform_sync_status`** — the entire staged-rollout safety
model from `ebay-listing-sync.md` (kill switch → `sync_enabled` →
`status='active'`) didn't apply to this feature at all. Confirmed the
real consequence: the listing used for every test this session
(`335662210469`) has `sync_enabled=false` on all 244 rows (never opted
into anything) — `--ebay-pushprices` would have pushed real changes to it
anyway if run for real.

**Fix, keeping resolve vs. push separate per Fei's call** (resolution
stays unfiltered — useful for previewing a listing's prices before
deciding to turn sync on for it):
- Extracted the kill-switch check out of `ebay_listing_sync.py`'s
  `_resolve_scope` into a standalone `platform_sync_allowed(cur, platform,
  account)`, reused by both features instead of duplicated.
- `ebay_pushprices.py`'s `_compute_changes` now returns a 3-tuple
  (`resolved, changes, skipped_ungated`) — `changes` only includes rows
  that are BOTH stale AND gated-in (`sync_enabled` + `status='active'` +
  kill switch); `skipped_ungated` surfaces rows that would've changed but
  aren't gated in, with a reason, instead of silently dropping them.
  Verified: the never-opted-in test listing now correctly shows
  0 changes / 244 skipped (`sync_enabled=false`); enabling `sync_enabled`
  on 3 rows (rollback-only test) correctly flips exactly those 3 to
  eligible, the other 241 stay skipped.
- Added a `window.confirm()` to the web UI's Push button (there was none —
  this is the first place in the whole app that writes to a live eBay
  listing) and a "Synced?" column per row so the grid is transparent about
  which rows are actually gate-eligible, not just which have a pending
  price change. The pending-count banner now separately reports "N need
  push" (gated + stale) vs. "N changed but not sync-enabled (won't push)".

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
