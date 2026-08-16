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

## HANDOFF — three confirmed, not-yet-built items (2026-07-22)

Fei reviewed a real listing (`336204674240`, "Mega Evolution Base Set
Common Listing") and confirmed all three of the following. None of this
has been implemented yet — start here in the next session.

### 1. Tier gap bug in `COM-IN-CommonUncommon` profile (data fix, not code)
Confirmed live: profile `eb1cb1c4-8789-403b-900a-ec21433d7f2e`'s tiers
have 1-cent dead zones between brackets — e.g. `[0.00,0.10)` then
`[0.11,0.20)`, nothing covers exactly `0.10`. Cards landing exactly on a
boundary cent value (`019/132 Vulpix`, `031/132 Chi-Yu`, both market
`$0.10`) fall through `resolve_listing_prices()`'s tier match and silently
hit the `market×2+1` default-formula fallback ($1.20) instead of the
group's real price ($0.99), showing `price_source='default'` instead of
`group:...` even though the card is grouped and the group has a profile.
Same gap pattern likely repeats at every other boundary in this profile
(`0.20/0.21`, `0.25/0.26`, `0.35/0.36`, `0.50/0.51`, `0.75/0.76`,
`1.00/1.01`) and possibly other profiles too — worth auditing all
profiles' tiers for `max_market(n) != min_market(n+1)` gaps, not just
patching this one profile. Fix is a data edit (`UPDATE
pricing_profile_tiers SET min_market = ...` to close each gap), no schema
or RPC change needed. Do this check/fix first since it's cheap and
explains a real user-visible discrepancy.

### 2. Resolved Qty column + "Manual Pin Qty" repurposing of `quantity_limit`
Two parts, different risk levels:

- **Display (low-risk, do first)**: `resolve_listing_prices()` already
  returns `quantity_limit` (and `available_qty`/`low_stock_qty`) but
  `listing-pricing.js`'s `rowHTML()` doesn't show a resolved/effective qty
  anywhere. Add a column computing the same thing `ebay_pushprices.py`
  already computes at push time: `max(available_qty - (low_stock_qty ??
  0), 0)`, then capped by `min(..., quantity_limit)`. Pure display, no
  schema change, no ambiguity — safe to just build.

- **"Manual Pin Qty" (needs a data reset first — confirm before running)**:
  Fei wants `platform_listings.quantity_limit` turned into a genuine
  per-card override (like `manual_price` is a price pin), rather than its
  current meaningless state — confirmed live that **all 9,363 rows** carry
  `quantity_limit=18`, purely an old column default, never a real
  per-card decision. Plan:
  1. Reset all 9,363 rows' `quantity_limit` to `NULL` (this is the "confirm
     before running" step — it's a bulk update across every
     `platform_listings` row; get an explicit go-ahead in-session even
     though the design itself is already agreed, since irreversibly
     wiping a column's data on 9k+ rows deserves a last check).
  2. Flip `resolve_listing_prices()`'s precedence for `quantity_limit` from
     `COALESCE(v_default_quantity_limit, l.row_quantity_limit, 24)`
     (template wins) to `COALESCE(l.row_quantity_limit,
     v_default_quantity_limit, 24)` (row-level pin wins when set, template
     default otherwise) — this is the mirror image of the base_price/pin
     precedence already used for `manual_price`.
  3. Add an editable "Qty Limit" input next to the existing pin-price
     input in `listing-pricing.js`, writing to `platform_listings.quantity_limit`.

### 3. Formula-based tiers in `pricing_profile_tiers`
Add nullable `multiplier numeric` and `plus numeric` columns to
`pricing_profile_tiers`. Resolution rule in `resolve_listing_prices()`:
if a tier's `list_price` is set, use it as today (flat price); if
`list_price` is `NULL` but `multiplier` is set, compute
`market_price * multiplier + plus` instead. Lets one profile mix flat
tiers for low brackets with an open-ended formula tier for "anything over
$2" (`min_market=2.00, max_market=NULL, list_price=NULL, multiplier=2,
plus=1`) — replicates the old `ultra_rare_rule` formula behavior, scoped
per-tier instead of profile-wide. Needs a UI toggle in the tier editor
(flat price vs. formula) instead of the current single price field. Purely
additive — no existing data affected, safe to build without a confirmation
step.

**Suggested build order**: #1 (data fix) → #2 display half → #3 (schema +
RPC + UI, additive) → #2 pin half (needs the reset confirmation).

### Build log (2026-07-22, session 2)
- ✅ **#1 fixed** (data only, no migration file — a straight `UPDATE`, not
  DDL): closed all 7 boundary gaps in `COM-IN-CommonUncommon`
  (`eb1cb1c4-...`) by setting each tier's `min_market` to the prior tier's
  `max_market` (`0.11→0.10`, `0.21→0.20`, `0.26→0.25`, `0.36→0.35`,
  `0.51→0.50`, `0.76→0.75`, `1.01→1.00`). Audited all 3 live profiles first
  — `double_rare_common` / `double_rare_rh_ur` had no gaps, only this one
  needed it. Verified against the real listing (`336204674240`): Vulpix and
  Chi-Yu (both market `$0.10`) now resolve at `$1.37` via
  `group:b8d67a05-...` instead of falling through to the `$1.20` default.
- ✅ **#2 display half**: added a "Resolved Qty" column to
  `listing-pricing.js`'s roster table, computed the same way
  `ebay_pushprices.py` computes `qty_to_push`
  (`max(available - low_stock_qty, 0)`, capped by `quantity_limit`).
  **Found and fixed a related latent bug while doing this**: `needsPush()`
  (drives the "stale row" highlighting and the Push button's pending count)
  computed the low-stock-gated qty but never applied the `quantity_limit`
  cap, so it could flag a row as needing a push based on a qty that didn't
  match what a real push would actually send. Refactored it to share the
  new `resolvedQty()` helper instead of duplicating the calculation.
- ✅ **#3 formula tiers**: migration 007 adds nullable `multiplier`/`plus`
  to `pricing_profile_tiers`, drops `list_price`'s `NOT NULL`, adds
  `chk_tier_price_or_formula` (`list_price IS NOT NULL OR multiplier IS NOT
  NULL`) so a tier can't be saved with neither. `resolve_listing_prices()`
  tier lookup now branches: flat `list_price` when set, else
  `market_price * multiplier + plus`. Verified via a rollback-only test
  against the real listing (temporarily pointed a live group at a scratch
  formula profile, confirmed `market×3+0.50` computed correctly and still
  composed correctly with the migration-006 `base_price` floor, e.g. `$0.06
  → $0.68 formula → floored to $0.99`; rollback left no trace). Added a
  flat/formula toggle to **both** tier-editing surfaces in the web app —
  Configuration's per-profile Tiers modal (the primary path) and the
  inline "New profile" quick-create modal on the Listing pricing page —
  since both write directly to `pricing_profile_tiers` and the inline one
  would otherwise silently drop formula-only rows (its old filter required
  a numeric `list_price` on every row). `tiersSummary()` and the tier
  table now render formula tiers as `market × N (+ $P)` via a new
  `tierPriceLabel()` helper instead of showing a broken price.
- ✅ **#2 pin half**: Fei gave the explicit go-ahead in-session. Reset
  confirmed live (9,363 rows had a value beforehand, 0 after). Migration
  008 flips `resolve_listing_prices()`'s `quantity_limit` precedence to
  `COALESCE(row_quantity_limit, v_default_quantity_limit, 24)` (row pin
  wins). Verified via rollback-only test: pinning one row to `7` made the
  RPC return `quantity_limit=7` for it; rollback confirmed 0 rows still
  carry a value. Added a "Qty Limit pin" input in `listing-pricing.js`,
  same change-on-blur pattern as the existing manual-price pin input.
  **Caught while wiring it**: the `platform_listings` select used to
  populate `state.listingRowsByPLId` didn't list `quantity_limit` in its
  column list — the new input would have always rendered blank even for a
  row with a pin set. Added it to the select.
- No live browser/eBay test of the new UI (same standing limitation — no
  JS runtime available in this environment).

### Four UI requests from a live screenshot review (2026-07-22, session 3)
Fei reviewed the roster grid live (group `COM-IN-DoubleRare` on listing
`336204674240`) and asked for four things:
1. **Edit a profile's tiers without leaving the Listing pricing page** —
   added an "Edit tiers" button next to the group's Profile picker
   (only shown once a profile is assigned), opening a standalone modal
   (`openEditTiersModal`) that mirrors Configuration's tier editor
   (including the flat/formula toggle) rather than importing it — same
   duplication convention already used for the inline "New profile" modal.
2. **Card images, hover-to-enlarge** — migration 009 adds `image_url`
   (`COALESCE(card_master.image_url_own, image_url)`) to
   `resolve_listing_prices()`. Added a 40×56 thumbnail column and ported
   picking.js's exact hover-zoom pattern (`img.card-thumb` + one shared
   floating preview element cached on `window`).
3. **Group-level select-all** — checkbox in each group's header (including
   the "(no group)" bucket) that toggles every row checkbox within that
   specific group's table, scoped via `.closest('.lp-group')` so it
   doesn't touch other groups' selections.
4. **Root cause of the "Common Holo" label on a Mega Evolution promo**:
   `derived_label` (rarity + foil_type, e.g. "Common Holo") is a leftover
   identity from the old auto-grouping design, where it doubled as a
   row's whole identity. `rowHTML()` shows `platform_listings.external_id`
   (the real eBay title) for `active` rows, but `queued` rows have no
   `external_id` yet and were falling back to `derived_label` — reading as
   flatly wrong once grouping became manual and every row is one specific
   card. Migration 009 also adds `card_name`/`card_number` from
   `card_master`; `rowHTML()` now shows `card_number card_name` (falling
   back to `derived_label` only if a row somehow has no card identity),
   with `derived_label` demoted to a small subtitle under it.
   **Found something worth flagging while verifying this against the real
   listing**: the queued row in question actually resolves to
   **Charcadet #22** in `card_master`, not a Mega Evolution promo card —
   and its `image_url` is `null` (no stock or own photo on file). Either
   the wrong card got matched in "Add card to listing"'s search, or the
   real promo variant isn't in the catalog yet — worth a manual check now
   that the grid shows the real name instead of masking it as "Common Holo".

### Set column + queued-card pins + the "push live" gap (2026-07-22, session 4)
Fei asked two follow-ups from the live grid: show the card's set, and why
`Low-stock qty`/`Manual pin`/`Qty Limit pin` are grayed out for queued
rows plus what the workflow is to get a card out of `queued`.

1. **Set column** — trivial, `resolve_listing_prices()` already returned
   `set_name` (migration 005); just wasn't rendered. Added to the grid.
2. **Root cause of the grayed-out queued inputs**: `manual_price`/
   `low_stock_qty`/`quantity_limit` lived on `platform_listings`, which
   only has a row once a card is actually live — a queued row has nowhere
   to store a pin. **Root cause of "no workflow to un-queue"**: the only
   code path that flips `queued` → `active` is `_do_promotions()` in
   `ebay_pushprices.py`, and it explicitly no-ops unless the roster's
   total row count exceeds 250 — this listing has ~12 rows, so a queued
   card here could never be promoted by anything that exists today.
3. **Scoped two features to fix this** (not yet built — schema/data layer
   done this session, the actual eBay-write code is next session):
   - A **per-card "Push live" button** for one queued row: adds just that
     one new `<Variation>` to the live listing (reusing the deep-copy
     helpers in `ebay_variations_xml.py`), touching no other variation's
     price/qty on eBay.
   - The **general Push button auto-promoting queued rows** whenever
     there's room under 250 (not just when something sold out at cap) —
     `_do_promotions()` needs a "free slot, no deletion needed" branch
     added alongside its existing "at cap, swap a sold-out row" branch.
     Confirm dialog will report the promotion count explicitly (e.g. "3
     price/qty changes + 2 cards going live for the first time").
4. **Pin storage — real architecture discussion, not just a data fix**:
   Fei's ask ("I should be able to edit these regardless of status")
   forced the actual design question of where a pin belongs. Landed on:
   `platform_listings` stays a pure live-eBay-state mirror (external_id,
   pushed_*, sync_enabled — nothing conceptually possible for a card
   that's never been pushed); `listing_card_assignments` (the roster row,
   which exists for every card regardless of status) becomes the single
   source of truth for `manual_price`/`low_stock_qty`/`quantity_limit`.
   No copying needed on promotion — same `id` before and after, so a pin
   set while queued just keeps applying once live. Migration 010:
   - Added the three columns to `listing_card_assignments`.
   - Dropped them from `platform_listings` (confirmed zero live data
     first — no row anywhere had a non-null value in any of the three).
   - `resolve_listing_prices()` now reads pins straight from `lca.*` (the
     `platform_listings` join that existed only for these three columns
     is gone entirely). Also exposes the raw pin values as new output
     columns (`manual_price`, `row_low_stock_qty`, `row_quantity_limit`)
     alongside the existing resolved `low_stock_qty`/`quantity_limit`, so
     the UI can show "what's actually pinned" without a second query.
     **Incidental fix**: the low-stock input previously displayed the
     RESOLVED value (profile-default fallback included) as if it were the
     raw override — saving without touching it could silently write a
     profile's default as an explicit per-row pin. Now shows the true raw
     value, same as manual_price/quantity_limit already correctly did.
   - `ebay_pushprices.py`'s promotion `INSERT` no longer writes
     `quantity_limit` into the new `platform_listings` row — nothing to
     copy, the pin already lives on the roster row being promoted.
   - Web UI: all three pin inputs are now always-editable regardless of
     status, reading/writing `listing_card_assignments` via `row_id`.
   - **Real regression caught and fixed before it shipped**: initially
     dropped `quantity_limit` from `platform_listings` without grepping
     the whole web app first — `inventory.js` (a completely separate,
     pricing-system-unrelated page) has its own "Edit listing" feature
     that reads/writes `platform_listings.quantity_limit` directly as
     genuine live-eBay state (independent of the pricing pin concept).
     Considered pointing `inventory.js` at `listing_card_assignments`
     instead, but only 243 of 9,363 `platform_listings` rows (2.6%) have
     a roster row at all — the other 97.4% were never added to a
     template, so there'd be nothing to point at for almost every
     listing. Fei's call: drop `quantity_limit` from `inventory.js`
     entirely instead (three separate UI locations — two edit panels, one
     "Add listing" modal) rather than restore it on `platform_listings`
     or half-wire it through the roster table.
   - **Known side effect, disclosed and accepted**: the OLD, already-
     superseded `--ebay-recalc-prices` pipeline
     (`ebay_listing_sync.py::_apply_bumps`) reads `quantity_limit` off a
     raw `SELECT pl.*` for its low-stock-bump feature. After the drop,
     that key is simply absent, so `.get()` returns `None` instead of
     erroring — `low_stock_bump` goes permanently inert on that pipeline
     (no crash). Not patched, since that pipeline is being moved away
     from anyway.
   - Open question raised by Fei, explicitly deferred: how will a
     "single listing per card" roster scale if it means hundreds of
     `listing_card_assignments` rows per card? No action taken — revisit
     when it's actually needed.
5. ✅ **Built**: both eBay-write features from item 3.
   - **Real bug caught and fixed while building this**: the ORIGINAL
     `_do_promotions()` executed its `INSERT`/`UPDATE` writes immediately
     — even during `--dry-run`, and even before the actual eBay
     `ReviseItem` call had happened at all. That meant a dry-run (or a
     push whose later POST failed) would still leave the DB believing a
     card had gone live, when eBay never received anything. Refactored
     `_do_promotions()` and the new `_stage_promotion()` helper (factored
     out, shared by both the general push and the new per-card push) to
     only *mutate the in-memory XML* and return `(promotions,
     pending_writes)` — the caller now only executes `pending_writes`
     after a real, successful `ReviseItem` POST. The new
     `platform_listings` row's id is generated client-side
     (`uuid.uuid4()`) specifically so the INSERT + both dependent UPDATEs
     can be pre-built as plain parameterized tuples without a `RETURNING`
     round-trip forcing immediate execution. Also fixed a smaller latent
     bug in the same pass: the original promotion INSERT never set
     `pushed_price`/`pushed_qty`/`pushed_at`, so a just-promoted row would
     immediately show as "stale, needs push" again on the very next page
     load even though it had just been pushed — now set directly in the
     INSERT.
   - `_do_promotions()` now has two cases instead of one: **direct**
     (active count under eBay's 250-variation cap — promote as many
     queued rows as fit, no deletion needed) and **swap** (at cap — the
     original one-for-one "delete a sold-out row, promote the next
     queued row" logic). The old gate (`total_roster > 250`, counting
     queued+active+sold_out_retained combined) was actually wrong for
     the real constraint, which is the LIVE variation count — fixed to
     check active count specifically.
   - New `push_single_card_live(row_id, ...)` in `ebay_pushprices.py` —
     pushes exactly one queued row live, refuses if the listing's live
     variation count is already at 250 (with a clear error pointing at
     the general push's swap logic instead), reuses `_stage_promotion`
     so the two paths can never compute a promotion differently.
   - CLI: `--ebay-push-card --row-id <uuid> [--account N] [--dry-run]
     [--quiet]`, wired in `main.py`.
   - API: new `POST /api/push-card` in `picking_api.py` (same token
     auth as `/push-prices`, separate lock so a single-card push doesn't
     queue behind a full-listing push or vice versa).
   - Web UI: "Push live" button on each queued row (replaces the "n/a"
     that used to sit in the Synced? column) — does a silent dry-run
     first to show exact card/price/qty in the confirm dialog, then
     pushes for real. General Push button now also does a silent
     dry-run first specifically to build the count-aware confirm text
     Fei asked for ("3 price/qty changes + 2 cards going live for the
     first time"), and is no longer disabled just because there are zero
     active-row price/qty changes — a queued row alone is now enough to
     enable it (the exact promotable count isn't knowable client-side
     without asking the Python side, which the pre-push dry-run does).
   - **Not tested against live eBay** — same standing limitation as
     every prior pass on this feature: no browser or eBay API access in
     this environment. Verified via `py_compile` (syntax only) and
     careful manual re-read; a real dry-run against a live listing is
     the next verification step before trusting this against real data.

### Manual market-price edit (2026-07-22, session 5)
Fei asked for a way to edit market price directly on the Listing pricing
page. First version wrote manual edits to a sentinel `condition='manual'`
row in `market_prices`, isolated from `v_inventory` (Inventory tab) and
every other consumer — Fei caught this immediately ("does it get
reflected in the real table?") and confirmed the actual intent: a manual
edit should BE the real market price everywhere, not a pricing-page-only
override. Checked live data before redoing it: all 8,182 existing
`market_prices` rows use `condition='Near Mint'`, no exceptions — no real
per-variant condition ambiguity to resolve, so the fix is simple. Final
migration 011: the web UI upserts directly into the variant's
`condition='Near Mint'` row (`source='manual'`) — the exact same row
`v_inventory` already reads via `mp.variant_id = i.variant_id AND
mp.condition = i.condition`. `resolve_listing_prices()` needs no special
casing for this at all (reverted the "prefer source='manual'" ordering
from the first attempt) — a manual edit just becomes the newest row for
that variant, which its existing "most recent updated_at" lookup already
picks up naturally. Still exposes `market_price_source` (`mp.source`
directly) so the UI can show a "manually set" badge. Clearing the input
now deletes the row entirely (no separate "automatic" value to fall back
to anymore) rather than reverting to something else.

Verified with a rollback-only test against the real listing, checking
BOTH `resolve_listing_prices()` and `v_inventory` in the same
transaction: both showed the manually-set $8.88 before rollback,
confirming it actually reaches the Inventory tab now, not just the
pricing grid.

Web UI: the "Market" column is now an editable input (previously plain
text), writes to `market_prices` via `variant_id` + `condition='Near
Mint'`, highlighted (purple border) when manually set. Confirmed
`market_prices` already has proper RLS (predates the Listing Pricing
System — "authenticated only" policy, same as everywhere else) — no gap
to fix this time, unlike the 6 newer tables from earlier sessions.

### Remove-from-listing + permanent roster removal (2026-07-22, session 5)
Fei asked for the reverse of "Push live" — pulling one card's variation
off a live listing. Two distinct actions, confirmed explicitly ("go back
to queue, until I permanently remove it from roster"):
- **Remove from listing** (active → queued): `remove_single_card_live()`
  in `ebay_pushprices.py`, the mirror image of `push_single_card_live()`
  — deletes only that one `<Variation>` (`mark_variation_deleted`, the
  same helper the 250-cap swap already uses), touches nothing else on
  the live listing. On success the roster row goes back to `'queued'`
  (`platform_listing_id` cleared) rather than being deleted, so it can be
  pushed live again later with zero extra setup — the old
  `platform_listings` row is kept as history (`status='delisted',
  sync_enabled=false`) instead of deleted. CLI: `--ebay-remove-card
  --row-id <uuid>`. API: `POST /api/remove-card` (own lock, same auth).
- **Remove from roster** (permanent): plain `DELETE` on
  `listing_card_assignments`, client-side, no eBay call — deliberately
  only ever offered for `'queued'`/`'sold_out_retained'` rows, never
  `'active'`, so a live eBay variation can never end up with no roster
  row tracking it (which would silently stop it from ever being priced
  or synced again). An active row has to go through "Remove from
  listing" first.

Web UI: added a dedicated "Actions" column (previously the "Push live"
button lived awkwardly in the Synced? column, which now just shows
plain yes/no/n/a again). Active rows get a "Remove" button; queued rows
get both "Push live" and "Remove from roster"; sold_out_retained rows
get only "Remove from roster" (nothing left to push live for that
specific row — it's a historical record of a card that already got
swapped out).

Not tested against live eBay — same standing limitation as every other
piece of this feature.

**Real bug found immediately after Fei tested this live**: the "Import
into roster" banner ("N existing platform_listings row(s) for this Item
# aren't on the roster yet") counted ALL `platform_listings` rows for the
listing_id minus whatever the roster currently points to — including
`status='delisted'` rows that Remove-from-listing intentionally leaves
behind as history. Since a removed card's roster row clears
`platform_listing_id` to `NULL`, its old delisted row stops being
"pointed at" and got miscounted as newly-unimported, even though the
card was already correctly represented as a `queued` roster row.
Confirmed live against Fei's test case (`336204674240`, Charcadet #22):
delisted row with no roster row pointing at it, roster row correctly
`queued` with `platform_listing_id=NULL` — the underlying remove logic
was fine, only the banner's count was wrong. Worse, `importExisting()`
had the same blind spot — clicking "Import into roster" on that phantom
count would have inserted a NEW `active` roster row pointing at the dead
delisted listing, creating a duplicate entry for a card that isn't
actually live. Fixed both queries (the count and the import candidate
list) to exclude `status='delisted'` rows.

### Custom variation name + promo set-prefix fix (2026-07-22, session 5)
Fei clarified the Charcadet #22 situation was never a wrong-card bug —
it's a real, correctly-added promo card, intentionally bundled into the
Mega Evolution base-set listing since it came with that product. The
actual problem: the rendered eBay variation name was broken
("22/ Charcadet" — dangling slash, no denominator), because
`card_sets.set_prefix` (already correctly populated: `'MEP'` for Mega
Evolution Black Star Promos, `'SVP'` for Scarlet & Violet Black Star
Promos) was never read by `_render_variation_name()` at all — it only
ever knew `{number}`, `{number:pad}`, `{set_total}`, `{name}`, `{suffix}`.
Promo sets normally have a prefix but no `total_cards` (unlike numbered
main sets), which is why the numbered format broke specifically for them.

Two-part fix:
1. **`_render_variation_name()`** (`ebay_listing_sync.py`): added a
   `{prefix}` token, and defaults to `"{prefix} {number} {name}
   {suffix}"` when a card's set has `set_prefix` but no `total_cards`.
   **Caught a real bug while testing this against the live listing**: my
   first attempt gated the promo-format switch on the template's
   `name_format` being `NULL` — but confirmed live that all 3 existing
   templates have `name_format` explicitly set to the literal string
   `"{number}/{set_total} {name} {suffix}"` (the web UI's create/edit
   form writes this exact default verbatim unless a user types something
   else — never actually `NULL`), so the `NULL` check never fired.
   Fixed by treating "still equal to that literal default string" the
   same as "no override" — a genuine customization (anything else) is
   still respected. Verified via a real `--ebay-push-card --dry-run`
   against the live Charcadet row: went from `'22/ Charcadet'` to the
   correct `'MEP 22 Charcadet'`.
2. **`custom_name` column** (migration 012) on `listing_card_assignments`
   — a genuine per-card override, same pin pattern as
   `manual_price`/`low_stock_qty`/`quantity_limit`. Format-string tokens
   can't cover every real convention Fei described (word order flips for
   alpha-sorted listings, literal "Black Star Promo" wording that isn't
   a computed value) — `custom_name`, when set, is used verbatim by
   `_stage_promotion()` instead of calling `_render_variation_name()` at
   all, so it's honored by both the per-card push and the general push's
   promotion path identically. Exposed via `resolve_listing_prices()` as
   a new raw passthrough output column (same convention as
   `manual_price` etc.). Web UI: only queued rows show an editable
   "custom name" input (in place of the plain label) — active rows keep
   showing `platform_listings.external_id` as before, since renaming an
   already-live variation is a separate, deliberate action
   (`rename_variation.py`) outside this feature's scope.

Where names are stored, for reference (came up mid-conversation): the
format template lives in `listing_templates.name_format`; the source
data (`card_master`, `card_sets`, `card_variants`) is read fresh every
render, nothing cached; the final rendered name only persists once a
card actually goes live, as `platform_listings.external_id`, set once at
push/promotion time and treated as sticky afterward.

### Stage a picture for eBay (EPS) before a card goes live (2026-07-22/23, session 6)
Fei wants to attach a photo to a new variation as part of adding it —
initially framed as "planning," landed on: click a queued card's
thumbnail, provide an image URL, upload it to eBay's own hosting (EPS)
right now, and have it attach automatically the next time that specific
card is actually pushed live. Explicitly **not** the R2/card_master
catalog-photo pipeline (that's a separate, later plan) — this only ever
touches eBay's own image hosting.

**Investigated first, before designing anything**: confirmed eBay's
Trading API does not accept an arbitrary external URL for a
variation-specific picture — `VariationSpecificPictureSet` needs an
EPS-hosted URL, so "fetch the bytes, then multipart-upload to EPS" is
mandatory, not just the fallback path. That exact mechanism already
existed and was proven working, just as an uncommitted, one-off script
(`upload_listing_a_images.py`) never wired into the real codebase.

Built:
- **New `importer/ebay_pictures.py`** — promotes the proven
  `upload_picture_from_url()`/EPS-multipart-upload logic out of the
  one-off script into a real, reusable module. Also adds
  `upload_picture_bytes()` (skips the download step, for a future
  direct-file-upload path — see below).
- **`set_variation_picture()`** (`ebay_variations_xml.py`) — adds/updates
  one `<VariationSpecificPictureSet>` entry inside `<Variations>
  <Pictures>`. **Real ordering bug caught before it shipped**: this
  function and `add_variation_row()` both simply append to `variations`
  — if a batch promotes several queued rows in one push (the "room under
  250" case), naively calling `set_variation_picture()` right after each
  `add_variation_row()` inside the same loop would interleave `<Pictures>`
  between `<Variation>` elements, which is malformed. Fixed by
  restructuring so `_stage_promotion()` only returns the staged picture
  URL (never applies it), and both callers (`_do_promotions()`,
  `push_single_card_live()`) apply every picture in one pass, strictly
  after every variation in the batch has already been added.
- **Migration 013**: `listing_card_assignments.eps_picture_url` (nullable)
  — same staged-pin pattern as `custom_name`. Exposed via
  `resolve_listing_prices()`.
- **`stage_card_picture(row_id, source_url, ...)`** (`ebay_pushprices.py`)
  — uploads now, writes `eps_picture_url`, only for `status='queued'`
  rows (nothing live to stage against otherwise — active-row support
  explicitly deferred, Fei's call). CLI: `--ebay-stage-picture --row-id
  <uuid> --image-url <url>`. API: `POST /api/stage-card-picture`.
- **Verified against the real Charcadet row, not just a rollback test**:
  ran the actual CLI command with a real public image URL, got back a
  genuine `i.ebayimg.com`-hosted URL from eBay, confirmed it persisted,
  then ran `--ebay-push-card --dry-run` and confirmed the picture-attach
  code path executes cleanly alongside the promo-naming fix with no
  errors. Cleared the test `eps_picture_url` back to NULL afterward so
  the real row doesn't end up with a stray test image staged on it.
- **Web UI**: thumbnail is clickable only on queued rows (shows the
  staged EPS picture if one exists, falling back to the catalog image,
  with a small checkmark badge when staged) — opens a URL-input modal,
  calls the new endpoint, refreshes.
- **Local-file upload, built same session**: installed `python-multipart`
  (added to `requirements.txt`), new `POST /api/stage-card-picture-file`
  (separate route from the URL one — FastAPI can't mix a JSON body with
  multipart `Form`/`File` params on one endpoint) using the
  already-written `upload_picture_bytes()`. Web modal now offers both
  URL and file, mutually exclusive (picking one clears the other).
- **Still deferred**: staging/immediately-revising a picture for an
  already-active row (Fei's explicit call — queued-only for now).

### Duplicate listing template (2026-07-23, session 6)
Fei asked for a "duplicate template" action, listing_id left blank
(since the copy isn't tied to a real eBay listing yet). Confirmed scope:
config only, no roster — reuses the existing template create/edit modal
entirely (`openTemplateModal()` gained an optional `duplicateFromId`
param that seeds every field from the source template except `name`
(" (copy)" appended) and `listing_id` (forced blank), while still
submitting through the normal INSERT path — no new modal, no new DB
logic, `listing_card_groups`/`listing_card_assignments` untouched so the
new template starts with a genuinely empty roster). "Duplicate" button
added next to "Edit" in the templates list.

### Fixed template deletion — the existing Delete button never actually worked (2026-07-23, session 6)
Fei asked for "a way to remove listing templates" — a Delete button
already existed (behind Edit), but a plain `DELETE` on
`listing_templates` was silently doomed for any template that had ever
been used: both `listing_card_assignments.template_id` and
`platform_listings.template_id` are `NO ACTION` FKs (only
`listing_card_groups.template_id` cascades). Confirmed live: **all 3**
of the app's real templates already have active roster rows, so the old
button had never actually succeeded for anything but a brand-new,
never-touched template. Fixed by having the delete handler show the
real counts (active/queued/sold_out_retained roster rows + referencing
`platform_listings` rows) in the confirm dialog, then clean up in the
correct order on confirm: detach `platform_listings` rows (kept as
history — `template_id` set to `NULL`, not deleted, since they're real
past-eBay-sync records) → delete the roster (`listing_card_groups`
still cascades on its own) → delete the template. Left "Delete" one
click deep behind "Edit" rather than promoting it to a direct list
button — matches the extra-friction pattern used for other
now-consequential actions this session.

### Bulk sync enable/disable (2026-07-23, session 6)
Came out of a "no response" report on the general Push button — turned
out to be correct behavior, not a bug: every row on the listings Fei
tested has `sync_enabled=false` (the staged-rollout kill switch from the
original design), so nothing was gated in to push, and 0-changes reads
as no visible response. Fei's ask in response: a way to bulk-toggle
`sync_enabled` for a multi-selection. Added "Enable sync"/"Disable sync"
buttons next to the existing "Assign selected to group" control, reusing
the same `state.selected` set. `sync_enabled` lives on
`platform_listings`, only meaningful for `'active'` rows (queued rows
have no `platform_listings` row at all) — a mixed selection silently
skips the queued ones rather than erroring, same tolerance the Push
button already has for a mixed roster.

### Available_qty didn't account for the same card being shared across listings (2026-07-24, session 7)
Fei flagged this directly: "Available" was computed as pure total unsold
inventory, with no awareness that the same card can be (and commonly
is) listed on more than one eBay listing at once — eBay caps a single
listing at 250 variations, so any set bigger than that gets split
across several listings, all drawing on the same physical stock.
Checked live before touching anything: confirmed widespread, not an
edge case — many variants already sit active on 2-3 different
`listing_id`s simultaneously. Every one of those listings was
independently treating the full inventory count as available to it
alone, meaning pushing quantity to more than one could push a combined
total exceeding actual stock.

Fix (migration 014): `available_qty` for the listing being resolved is
now total unsold inventory **minus** `quantity_listed` already committed
on *other* active `platform_listings` rows for the same variant (same
platform, any other `listing_id`), floored at 0. Confirmed with Fei:
counts ALL active listings regardless of `sync_enabled` — an ungated
listing's quantity is still genuinely live on eBay right now,
`sync_enabled` only gates whether *this* system keeps pushing further
updates to it. Queued (not-yet-pushed) rows elsewhere don't count —
nothing's actually claimed on eBay for those yet. This lives in
`resolve_listing_prices()`, the single place both the web grid and every
push path (`push_prices`, `push_single_card_live`, 250-cap promotion)
get quantity from, so the fix applies everywhere at once.

Verified against real shared data (not just a rollback test): variant
`0070c079-...` has 24 in total inventory, split 12/12 across listing
`336691613250` (no template) and `336691917730` (Pitch Black Common
Listing, has a template). Before this fix, the Pitch Black listing would
have shown 24 available (the full pool, ignoring the other listing's
claim); confirmed live it now correctly returns 12 — the actual
remaining amount, after accounting for what the other listing already
has out. Regression-checked the Mega Evolution listing (140 rows,
unaffected — no other listing shares those cards) to confirm the
non-shared case is untouched.

### Balance Qty across listings (2026-07-24, session 7)
Direct follow-up to the shared-inventory fix: given a card is already
fully claimed by one listing, how do you free some of it up for a new
listing? Fei's spec: a modal showing every listing (including ones with
no `listing_templates` row) that currently offers the card, an "evenly
split" option, per-listing editable qty, and a trigger to revise each
one on eBay directly.

Confirmed the key technical question live before building: revising one
existing variation's quantity needs nothing from the template/roster
system — `platform_listings.listing_id` + `external_id` + `account` +
`platform` is everything required to find the matching `<Variation>` and
update `<Quantity>`. New `revise_single_variation_qty()`
(`ebay_pushprices.py`) works identically for both cases; verified with
real dry-runs against both listings from the earlier shared-inventory
example (`336691917730`, which has a template, and `336691613250`, which
has none) — same code path, same result. CLI:
`--ebay-revise-qty --platform-listing-id <uuid> --qty <n>`. API:
`POST /api/revise-variation-qty`.

Web UI: a small "Balance" link next to the Available number (not the
Actions column — it's tied to the exact number it explains, and applies
regardless of row status) opens a modal that queries `platform_listings`
directly for every active row sharing that `variant_id` (LEFT-JOIN-style
lookup against `listing_templates` just for a display name, falling back
to "(no template)"), shows total inventory, lets you evenly split or
hand-edit each listing's quantity, blocks applying if the entered total
exceeds actual stock, confirms once with a plain-text summary of every
change before sending anything live, then applies only the listings that
actually changed — sequentially, with a per-listing ✓/✗ result so one
failure doesn't hide whether the others succeeded.

### Targeted row refresh instead of full-table reload on pin edits (2026-07-24, session 7)
Fei flagged that every pin edit (low-stock qty, manual price, qty
limit, market price, custom name) felt like "the whole screen
refreshes" — every one of those handlers called `loadListing()` after
saving, which re-fetches everything and rebuilds the entire 140+-row
table from scratch, losing scroll position and input focus each time.

Weighed two fixes with Fei before building: (1) delay the reload
(debounce/blur) — small change, but doesn't help since each pin is a
separate input, so editing several in a row still means several
reloads; (2) fully optimistic local update — instant, but risks
duplicating `resolve_listing_prices()`'s derived-value logic (tier
lookups, floors, the shared-inventory subtraction) in JS, exactly the
kind of drift this whole system has been built to avoid. Landed on a
third option: still re-resolve from the server after every save (so
derived values are always server-computed, never duplicated in JS), but
only patch that one row's already-rendered `<td>` cells in place
instead of tearing down and rebuilding the whole table.

New `refreshRowDerivedCells(container, rowId)`: re-runs
`resolve_listing_prices()`, updates `state.resolvedRows` in place, then
patches only the derived/non-input cells of that one `<tr>` (resolved
price, source badge, available qty, resolved qty, synced yes/no/n/a,
stale-row highlight, market-price-pin styling) by class hook
(`.lp-resolved-price-cell` etc., added to `rowHTML()`). Deliberately
touches no input's own DOM node and re-wires nothing — since only text/
attribute content changes, every existing event listener on that row
stays attached untouched. All 5 pin handlers now call this instead of
`loadListing()`. Known, accepted gap: the top "N need push" banner and
Push button's enabled state go slightly stale until the next full-page
action — left as-is since it's purely cosmetic; the actual push always
re-checks fresh via its own dry-run before doing anything, so nothing
unsafe can happen from a stale display count.

Commit message convention so far has been one commit per logical
fix/feature, matching this doc's dated sections.

### Extended targeted refresh to Balance Qty and Stage Picture modals (2026-07-24, session 7)
Fei asked whether the same full-reload issue applied to the other two
places that mutate a row and then close a modal: Balance Qty and the
picture-staging thumbnail upload. Both did — each ended its success
path with `root.innerHTML = ''; await loadListing(container);`,
same as the pin inputs before the fix above.

Stage Picture: `openStagePictureModal()` already received `rowId` as a
parameter, so its `loadListing()` call was swapped directly for
`refreshRowDerivedCells(container, rowId)`. To make the thumbnail
itself patchable, the thumbnail cell's markup (image + green "staged"
checkmark badge) was extracted out of `rowHTML()` into two shared
helpers, `thumbTitle(r)` and `thumbInnerHTML(r)`, and
`refreshRowDerivedCells()` now also patches `.lp-thumb-upload`'s
`title`/`innerHTML` using those same helpers — so a successful EPS
upload updates the thumbnail in place instead of needing a reload to
show the checkmark.

Balance Qty: the `<a class="lp-balance-qty-link">` in `rowHTML()` and
its click handler in `wireControls()` now carry `data-row-id`, threaded
through `openBalanceQtyModal(container, body, variantId, cardLabelText,
rowId)` into `renderBalanceQtyBody(...)`. The apply-success handler's
`loadListing()` call became `refreshRowDerivedCells(container, rowId)`.
Note this only patches the row for the *currently viewed* listing —
Balance Qty can revise quantities on other listings too, but those
aren't rendered in this table, so there's nothing to patch for them;
the current listing's row is the only one that needed a rendered patch.
The Cancel button no longer calls `loadListing()` at all — cancelling
makes no changes, so there is nothing to refresh.

### Duplicate pricing profile (2026-07-24, session 8)
Added a "Duplicate" button next to Edit/Tiers on the pricing profiles
table in the config page (`configuration.js`). Unlike duplicating a
listing template — where the roster is deliberately left empty because
it's large, specific per-card data — a duplicated pricing profile also
copies every tier. Confirmed with Fei: tiers ARE a profile's actual
pricing rules, so an empty duplicate would just fall back to the
market×2+1 default and be useless as a starting point. `duplicateProfile()`
inserts a new `pricing_profiles` row (name suffixed `(copy)`, auto-
incrementing to `(copy 2)`, `(copy 3)`... if that name's already taken)
with the same notes/default_low_stock_qty, then bulk-inserts copies of
every row in `source.tiers` against the new profile's id.

### Market price refresh — background jobs + Jobs page (2026-07-24, session 9)
CLAUDE.md's TODO list had an open, undecided item: a market-price refresh
command hitting the Pokemon TCG API for existing inventory, "still
deciding whether the CLI trigger should refresh all cards or be scoped
per-card/set." Resolved with Fei this session: scoped to one set or one
card, run as a background job (the API is slow — per-card round trips,
`timeout=30` — so a whole-set refresh can't block an HTTP request), and
Fei explicitly asked for this to be the first job on a general-purpose
**Jobs page**, not a one-off feature bolted onto Inventory.

**Backend — generic job registry.** `importer/job_runner.py` is new: an
in-memory `dict` of job_id → `{status, progress, result, error,
started_at, finished_at}`, `start_job(job_type, label, target, **kwargs)`
spins up a daemon thread and returns immediately, `update_job(job_id,
**progress)` lets the running job push progress, `get_job`/`list_jobs`
back the polling endpoints. State is in-memory only (lost on a
`picking_api.py` restart) — same accepted tradeoff as everything else in
that process; only the most recent 50 jobs are kept. Adding a future job
type is one more `target` function plus one more `POST` endpoint that
calls `start_job()` — it shows up in the generic job list automatically,
no frontend changes needed beyond that job's own "start" form.

**Backend — the actual work.** `importer/market_price_refresh.py`:
`refresh_market_prices(job_id, set_name, card_id, dry_run)`. Key design
point from Fei, caught before building: **the Pokemon TCG API returns
pricing for every foil-type variant of a card (normal/holofoil/
reverseHolofoil) in ONE response** (`get_card_by_id`), so the unit of
work is one `card_master` row (one `external_id`), not one
`card_variants` row — a card with holo + reverse-holo variants costs one
API call, not two; `extract_market_price(api_card, foil_type)` then
picks the right price out of that single cached response per variant.
Concurrency: `ThreadPoolExecutor(max_workers=15)` per Fei's ask — safe
because `db_cursor()` opens its own connection per call, no shared-state
locking needed for the concurrent upserts. Daily-freshness gate (Fei's
ask): a set-scoped refresh only includes a card if at least one of its
variants' `market_prices` row (`condition='Near Mint'`) is missing or
`updated_at::date < CURRENT_DATE` — a card is skipped only if ALL its
variants are already fresh today, since the underlying API call is
per-card and would refresh every variant anyway. A single explicit
`--card-id`/card-scoped refresh always calls the API regardless of
freshness — an explicit one-card click is a "do it now" action, not a
batch sweep. A per-card failure is caught inside `_refresh_one_card` and
reported, not raised — one bad card doesn't abort the batch, and
re-running the set refresh is itself the retry mechanism (only
still-stale cards get re-attempted).

**CLI**: `--refresh-market-prices --set NAME` or `--refresh-market-prices
--card-id UUID` (`--dry-run` lists which cards would be refreshed without
calling the API or writing anything).

**picking_api.py**: `POST /api/jobs/market-price-refresh` (body
`{set_name}` or `{card_id}`) starts the job and returns `{job_id}`
immediately — deliberately has NO lock, unlike every other endpoint in
this file, since concurrent refresh jobs for different sets don't
contend with anything. `GET /api/jobs` (list, for the Jobs page) and
`GET /api/jobs/{job_id}` (single job) are generic reads over
`job_runner`, not specific to this job type.

**Frontend — new Jobs page.** `jobs.js` + nav entry in `index.html`
(`#jobs`). Two "start" controls (set dropdown sourced from `card_sets`;
card search-as-you-type against `card_master.name ILIKE`), and a
"Recent jobs" table polling `GET /api/jobs` every 2.5s — polling
self-stops once no listed job is still `status: 'running'`, so this
isn't a permanent background timer (the codebase had no polling loop
anywhere before this; every other refresh action was manual-only).

**Inventory page quick-access entry points**, both firing the same
`POST /api/jobs/market-price-refresh` and just pointing the user at the
Jobs page for progress rather than duplicating a progress UI: a
"↻ Refresh prices for `<set>`" button next to the set filter (shown
only when a set is selected, mirroring the existing "Enable sync for
`<set>`" button), and a "↻ Refresh" link next to the Market price
display in the "List on platform" modal (`openListModal`), scoped to
that one card via `row.lots[0].card_id`.

### Push-live now populates ebay_listing_map (2026-07-25, session 10)
Fei hit a real "unmatched" order issue in the Issues tab (`item ...
has 100 variations; none named '114/086 Surfing Beach'`) and asked
whether adding the variation on eBay would auto-resolve it. Traced the
full chain in `importer/ebay_orders.py`: `_match_line()` (line 233)
matches a sold line's variation name **purely against our own
`ebay_listing_map` table** (`item_id + variation_name -> variant_id,
condition`) — it never calls eBay live, so adding a variation to the
live listing does nothing for this by itself.

Bigger finding: grepped the whole repo and **nothing wrote to
`ebay_listing_map`** — every reference was a `SELECT`. It's a
live-DB-only table (absent from `schema.sql`, per
`docs/plans/ebay-listing-sync.md`) that predates the roster/push
system; the newer push engine (`_stage_promotion()` in
`importer/ebay_pushprices.py`) writes new variations to
`platform_listings` instead, so any card promoted live through the
Listing Pricing System since the roster pivot would hit this exact
"unmatched" issue the first time it sold, with no path to auto-resolve.

Fixed at the source: `_stage_promotion()` (the one function both
`push_prices()`'s 250-cap promotion and `push_single_card_live()` call
to add a new `<Variation>`) now stages a third `pending_writes` entry —
an `ebay_listing_map` upsert (`ON CONFLICT (item_id, variation_name) DO
UPDATE`) keyed on the exact `promoted_name` string just written into
the live `<Variation>`, so it's guaranteed to match whatever eBay later
echoes back on a sale. `condition` is hardcoded `'Near Mint'`, matching
every other real row in this project. Verified the SQL directly against
the live DB (insert + re-run to exercise the `ON CONFLICT` path, then
rolled back — no residue). Like every other write in `_stage_promotion`,
this only lands after a successful live eBay Revise call, never on
`--dry-run` or a failed push.

This closes the gap going forward only — it does not backfill
`ebay_listing_map` for cards already pushed live before this fix, and
it doesn't resolve the specific Surfing Beach issue Fei hit (that card
isn't in the catalog at all yet — a different, uncatalogued card from
"Chaos Rising" (`me4`), not the same-named card already on file from
"Mega Evolution" — cataloging it is a separate, still-open task).

### Two ways to close an "unmatched" Issue, from opposite ends (2026-07-26, session 11)
Follow-up to the ebay_listing_map work above. Two real "unmatched" issues
this session needed different fixes — one card (Surfing Beach) didn't
exist in the catalog at all, the other (Drakloak) existed but was
missing one specific variant (Master Ball reverse holo) — and both
needed new inventory added, not just a mapping row. That's now covered:
found that the Staging Review page's approval RPC
(`push_staging_row_to_inventory`, live in Supabase) already inserts into
`ebay_listing_map` automatically whenever a staging row has
`source='ebay'` with `order_number`/`variation_name` set — nothing
exposed that from the "New Local Purchase" modal, which always
hardcoded `source: 'local'`. Added a "Link to an eBay listing" checkbox
per card in that form (`staging-review.js`); checked, it flows the
item ID/variation name into the staging row so approving it also maps
the variant — unchecked, behavior is unchanged.

That covers "needs new inventory too." The other half — a variant and
its inventory are ALREADY correct, and the gap is purely a missing or
wrong mapping (e.g. pushed live before this project's mapping-on-push
fix existed, or eBay's variation text got edited after the fact) —
doesn't belong in a purchase-entry form at all (would create a bogus
extra purchase just to sneak in a mapping). Added a second, narrower
tool for exactly that: an "Add mapping" button on each open `unmatched`
row in `issues.js`, opening a modal pre-filled with that issue's
`item_id`/`variation_name`, a card-name search against `v_card_variants`
(same pattern as Staging Review's autocomplete), and a Save that upserts
`ebay_listing_map` directly — no inventory/purchase touched. Confirmed
`ebay_listing_map`'s RLS ("authenticated only", `FOR ALL`) allows this
write. Neither tool touches the issue's own `status` — matches the
existing bookkeeping-only philosophy; `retry_open_issues()` on the next
`--ebay-pullorders` run is what actually re-matches and resolves it.

### Fixed push_staging_row_to_inventory() referencing a dropped column (2026-07-26, session 11)
Fei hit "Push failed: column 'quantity_limit' of relation
'platform_listings' does not exist" trying to push the Drakloak Master
Ball card through the new eBay-link checkbox. Root cause: migration 010
earlier this session moved `quantity_limit` off `platform_listings`
onto `listing_card_assignments`, but `push_staging_row_to_inventory`
(the Staging Review page's approval RPC — unrelated to
`resolve_listing_prices()`, so this slipped past every check made at
the time) still hardcoded `quantity_limit` (value `18`) in its
`platform_listings` INSERT and was never updated. Nothing had exercised
that code path since migration 010 landed, so it went unnoticed until
now. Fixed via `listing_pricing_migration_015_fix_push_staging_rpc.sql`
— dropped the column and its value from the INSERT, function otherwise
unchanged. Verified live: ran the RPC against a real staging row inside
a transaction, confirmed it now completes and returns
inventory_id/variant_id/purchase_id with no error, then rolled back
(no residue).

The flagged `list_price NOT NULL` risk did bite immediately after —
Fei hit the exact predicted error leaving Listing Price blank while
linking. Fixed in `staging-review.js`: the "Add to purchase" handler
now requires Listing Price whenever "Link to an eBay listing" is
checked (same validation pattern as the Item ID/variation name check),
and the field's label shows a red `*` while the checkbox is checked so
it's clear before submitting, not just after a failed save.

### Extended Add Mapping to also fix "listing_gap" issues (2026-07-26, session 11)
Fixing an `unmatched` issue via Add Mapping surfaced a second issue
type right behind it: `listing_gap` — "sale recorded, but no
platform_listings row for item X / variation Y". Traced in
`importer/ebay_orders.py:394-405`: the sale itself recorded fine
(inventory correctly deducted), but `record_sale()` also tries to
decrement `platform_listings.quantity_listed` for that exact
`(listing_id, external_id)`, and there was no row to decrement — this
variant was never pushed live through our roster/push pipeline, it
sold straight off a pre-existing eBay listing. Confirmed live (checked
by `variant_id` across ALL of `platform_listings`, not just scoped to
one listing): zero rows for any of the affected variants, even though
the parent listings themselves are heavily tracked (83 and 129 other
`platform_listings` rows respectively) — so it's specifically these
sold-but-never-onboarded variants that are missing, not a general
tracking gap on those listings.

Fei's first instinct ("add inventory before mapping") wasn't quite the
issue — inventory was already correct in every case here (that's why
the sale itself succeeded). The real gap: Add Mapping was deliberately
scoped to touch only `ebay_listing_map`, never `platform_listings`.
Also important: `listing_gap` is explicitly NOT auto-retried by
anything (per the code comment in `ebay_orders.py` — a retry would just
hit the sale's own dedup check and never actually create the row), so
unlike `unmatched` these don't clear themselves.

Added `openFixListingGapModal()` / a "Create listing" button for open
`listing_gap` rows in `issues.js`. Looks up the already-resolved
variant via `ebay_listing_map` (matching already succeeded — that's
why it's `listing_gap` and not `unmatched`), shows the card for
confirmation, and creates the missing `platform_listings` row (list
price pre-filled from the issue's own `sale_price`, quantity defaults
to the sold quantity, account pre-filled from the issue's own
`account` column — no extra lookups needed, `sync_enabled` defaults
false so this doesn't silently opt the card into active price-sync
management). Unlike Add Mapping, this modal explicitly marks the issue
`resolved` itself on success — since nothing else in the system ever
will for `listing_gap`, leaving status alone here would mean it stays
"open" forever even after the real fix landed.

### Duplicate listing template now copies groups + pricing profile links (2026-07-26, session 11)
Fei asked for grouping and pricing profile to carry over when
duplicating a template — previously deliberately config-only (roster
AND groups both empty). Revisited: the roster staying empty is still
right (it's genuinely per-listing, physical-card-specific data), but
leaving groups empty meant recreating the same rarity-tier
groups + pricing profile links by hand for every new listing built off
an existing one — usually the exact structure you want to keep.

`listing_card_groups(template_id, name, profile_id)` has no roster
dependency (roster rows point AT a group via `group_id`, not the
reverse), so copying is just: after the new template insert returns its
id, copy every `listing_card_groups` row from the source template with
the same `name`/`profile_id` but the new `template_id`. Groups land
empty (no roster rows reference them yet) — cards get assigned into
them normally as they're added to the new listing. Confirmed RLS
("authenticated only", `FOR ALL`) allows this insert directly from the
frontend.

### Balance Qty silently dropped out-of-stock listings from the pool (2026-07-31, session 12)
Fei reported "Evenly split" not actually splitting evenly for Morpeko
ex #55 (one Holo variant genuinely shared across two listings — Fei
confirmed that's intentional, not a cataloging gap). Root cause:
`openBalanceQtyModal()`'s `platform_listings` query filtered
`.eq('status', 'active')` — a listing that had already sold out
(`status='out_of_stock'`) was excluded from the modal entirely, so
"Evenly split" only ever divided inventory across whatever listings
were STILL active, permanently squeezing out any listing that had ever
hit zero. Fixed: query now uses `.in('status', ['active',
'out_of_stock'])` — confirmed live only three status values exist
(`active` 9212, `out_of_stock` 175, `delisted` 1) — `delisted` stays
excluded since that variation isn't live on eBay at all anymore for
`revise_single_variation_qty` to find. OOS rows now show a small
"(out of stock)" tag in the modal.

Found and fixed a related follow-on bug while in this code:
`revise_single_variation_qty()` (`importer/ebay_pushprices.py`) updated
`quantity_listed` but never touched `status` — so reviving an OOS
listing back to a positive quantity via Balance Qty left `status`
stuck at `'out_of_stock'`. Since `resolve_listing_prices()`'s
shared-inventory subtraction (migration 014) only counts an OTHER
listing's claimed quantity when its `status='active'`, a stale
`out_of_stock` status would make every OTHER listing sharing that
variant silently overcount how much was actually available. Now sets
`status = 'active' if new_qty > 0 else 'out_of_stock'` on every
revise, same convention as every other writer of that column.

### picking_api.py now reachable via Tailscale, not just the home LAN (2026-08-01, session 13)
Fei's pain point: every feature built this whole session (Picking
refresh, push-live, Balance Qty, market price refresh Jobs) routes
through `picking_api.py`, which only ever listened on the home LAN
(`http://192.168.1.186:8765`) — meaning every one of those actions
required physically being on the home WiFi. Discussed the standard
options for exposing a home-hosted service (mesh VPN like Tailscale,
Cloudflare Tunnel, classic port-forward+DDNS, or just moving the
service to a small VPS) and picked Tailscale: no public exposure at
all (unlike port-forwarding or Cloudflare Tunnel), no code changes
beyond swapping one constant, and free for personal use.

Installed via `winget install Tailscale.Tailscale` on the desktop,
logged in (account: flyworld2002), then installed + logged into the
same account on Fei's Mac and phone. Confirmed the existing Windows
Firewall rule for port 8765 (`netsh advfirewall firewall show rule
name="CBM Picking API"`) already allows Any/Any across all profiles,
so no firewall change was needed — end-to-end reachability verified
live from the Mac (`curl http://desktop-tu1m2fc.tail2c58d7.ts.net:8765/api/picking/health`
→ `{"ok":true}`).

Updated `PICKING_API_URL` in all four files that each keep their own
copy of it (`picking.js`, `listing-pricing.js`, `inventory.js`,
`jobs.js`) from the LAN IP to the desktop's stable Tailscale hostname
(`desktop-tu1m2fc.tail2c58d7.ts.net`) — this hostname never changes
even if the desktop's home IP does. The old LAN IP is kept as a
commented-out fallback line in `picking.js`/`jobs.js` (matching the
existing `localhost` fallback convention already there). Any device
triggering an action (browser tab, whatever) needs Tailscale running
and logged into the same account — this isn't a public URL, so a
device that was never added to the tailnet still can't reach it,
which is the intended security model (same "shared-secret header, no
public exposure" posture as before, just extended past the LAN).

### Frontend hosted on Cloudflare Pages + picking_api.py moved to HTTPS (2026-08-01, session 13 cont'd)
Followed the Tailscale work by hosting the frontend itself so Fei can
reach the whole app from anywhere, not just trigger picking_api.py
remotely while still running the SPA locally. Deployed
`card-board-mastermind-WebInvManagement` to Cloudflare Pages (git-
connected to the private repo via Cloudflare's GitHub App, auto-
redeploys on every push to `main`) — no build step, framework preset
"None", output directory `/`. Live at
https://card-board-mastermind-webinvmanagement.pages.dev.

Two real bugs surfaced going live, both fixed:
1. **Google sign-in redirected to `localhost` and failed.** Root
   cause wasn't the app code — `shared.js`'s `signInWithGoogle()`
   already computed `redirectTo` dynamically from
   `window.location.origin`, so it correctly requested the Cloudflare
   URL. The problem was Supabase Auth's **Redirect URLs allow-list**
   (Authentication → URL Configuration) only had `localhost` on it
   from prior local-only development — Supabase silently ignores an
   unlisted `redirectTo` and falls back to the default Site URL.
   Fixed by adding the Cloudflare Pages URL to that allow-list in the
   Supabase dashboard (no code change).
2. **Picking refresh failed with a generic "NetworkError."** The
   Cloudflare-hosted frontend is HTTPS; `picking_api.py` was still
   plain HTTP (even after the Tailscale-hostname swap) — browsers
   silently block an HTTPS page from calling an HTTP endpoint (mixed
   content), surfacing as an opaque fetch NetworkError rather than a
   clear "mixed content" message in most browsers. Confirmed via the
   browser's own console message ("Blocked loading mixed active
   content") once Fei checked it.

   Fixed by getting `picking_api.py` onto real HTTPS: enabled "HTTPS
   Certificates" for the tailnet (https://login.tailscale.com/admin/dns,
   one-time toggle), then `tailscale cert
   desktop-tu1m2fc.tail2c58d7.ts.net` provisions a genuine Let's-
   Encrypt-backed cert for the device's Tailscale hostname (trusted by
   browsers out of the box — no self-signed-cert warnings). Added
   `*.key`/`*.crt` to `.gitignore` immediately, before anything could
   accidentally stage the private key. `run_picking_api.bat` (what
   Task Scheduler actually launches — confirmed `picking_api.py`'s own
   `if __name__ == "__main__":` block is dead code for the real
   deployment) now passes `--ssl-certfile`/`--ssl-keyfile` on the
   uvicorn command line; `picking_api.py`'s own `__main__` block got
   the equivalent `PICKING_API_SSL_CERTFILE`/`PICKING_API_SSL_KEYFILE`
   env-var support too, for anyone running it directly. All four
   frontend files' `PICKING_API_URL` updated from `http://` to
   `https://` on the same Tailscale hostname. Verified end-to-end:
   `curl https://desktop-tu1m2fc.tail2c58d7.ts.net:8765/api/picking/health`
   succeeds with no `-k`/insecure flag needed (proves the cert chain
   is genuinely trusted, not self-signed), and the picking_api.py log
   confirms `Uvicorn running on https://0.0.0.0:8765` after restart.

   Also caught mid-fix: the earlier Tailscale-hostname edits to the
   four `PICKING_API_URL` constants (from the LAN-IP session) had
   never actually been committed/pushed — Cloudflare was still
   serving the old LAN-IP code the whole time, which is what the
   mixed-content error was actually reporting (`http://192.168.1.186`,
   not the Tailscale hostname). Both the stale LAN IP and the
   http-vs-https mismatch needed fixing together before this worked.

Cert renewal note: Tailscale auto-renews certs obtained via
`tailscale cert` in the background while `tailscaled` keeps running,
so manual re-provisioning should rarely be needed — but if
`picking_api.py` ever starts failing TLS handshakes, re-run
`tailscale cert desktop-tu1m2fc.tail2c58d7.ts.net` from the project
root and restart the `CBMPickingAPI` scheduled task.

### Major eBay quantity-corruption bug found and fixed (2026-08-01, session 13 cont'd)
While preparing to push the "Pitch Black Reverse Holo - Ultra Rare"
listing, Fei asked for a stock reconciliation against live eBay data
first. That turned into finding and fixing the biggest bug of the
project so far — a root cause that had been silently inflating live
eBay quantities on every push, for as long as this listing/push
pipeline has existed. Two distinct bugs, both real, both fixed:

**Bug 1 — double-counting QuantitySold.** eBay's Trading API has an
undocumented-in-this-codebase (but real, per eBay's own dev KB —
articles 1525/1526) behavior: whatever raw `<Quantity>` value you send
in *any* `ReviseItem`/`ReviseFixedPriceItem` call, eBay's backend adds
the variation's *current* `QuantitySold` on top before storing it. To
set "9 available," you send exactly `9` — eBay does the rest.
`push_prices()` didn't know this and was manually computing
`qty_sold + desired_available` before sending, so eBay added sold
*again* on receipt — every push stored `desired + 2×sold`, compounding
worse with every subsequent push. `revise_single_variation_qty()`
(Balance Qty) had never had this bug — it already sent the raw desired
value directly. Proved this empirically rather than trusting the KB
article's wording alone: sent a known raw value via
`revise_single_variation_qty`, then had Fei confirm eBay's Seller Hub
"Available quantity" column matched exactly. Fix: removed the manual
`qty_sold +` addition from both the "single" and multi-variation paths
in `push_prices()` — now sends `change["qty_to_push"]` directly, same
convention Balance Qty already used correctly. Also removed the now-fully-
unused `quantity_sold_by_var` threading through `_do_promotions()`/
`_stage_promotion()` and the `get_quantity_sold` import that went with it.

**Bug 2 — pass-through corruption (the bigger one).** This
codebase's revise pattern (documented in `ebay_variations_xml.py`'s
module docstring) is "deep-copy the whole `<Variations>` block, mutate
only what you mean to change, resend the rest byte-for-byte" — needed
because eBay treats an omitted `<Variation>` as a deletion. Turns out
eBay's sold-folding behavior from Bug 1 applies to *every* `<Variation>`
element in a revise call, not just the one(s) with an intentionally new
value — so every untouched, byte-for-byte-resent variation's stale raw
`<Quantity>` (which already had ITS old sold count baked in from a
previous revise) got sold-folded *again*, every single revise,
regardless of whether that variation was ever meant to be touched.
Proved this with an exact reproduction: revised 4 out-of-stock
variations to 0 one at a time; each one resent the others-not-yet-fixed
unchanged, and their stored quantity grew by exactly one more
`QuantitySold` per subsequent revise pass (1+1+1+1=4, 3+3+3=9, 1+1=2,
and the last one fixed had zero subsequent passes and came out
correct at 0 — an exact numeric match, not a coincidence). This also
explained why 51 of the 119 rows from an earlier "correctly fixed"
push looked wrong again minutes later: test/diagnostic revises run
*after* that push but *before* this fix existed had re-corrupted them
as collateral damage.

Fix: new `normalize_quantities(variations)` in `ebay_variations_xml.py` —
walks *every* `<Variation>` in the deep-copied block and rewrites its
`<Quantity>` to `existing_quantity - existing_sold` (true available),
run right after `deep_copy_variations()` and before
`strip_selling_status()` (needs to read `QuantitySold`, which that call
removes) and before any of the caller's own intentional mutations.
Wired into all 4 call sites in `ebay_pushprices.py` (`push_prices()`,
`revise_single_variation_qty()`, `push_single_card_live()`,
`remove_single_card_live()`). Re-verified the same 4-row sequence with
the fix in place — the first-fixed row now stayed correct through 3
subsequent unrelated revises, no re-inflation.

**Two follow-on design changes, both requested by Fei once the root
cause was clear:**
- `out_of_stock` platform_listings rows are no longer skipped by a
  push — they're gated in the same as `active` rows, with
  `qty_to_push` forced to `0`. Previously a listing that went OOS kept
  whatever stale (possibly corrupted) raw `<Quantity>` it last had,
  which a live buyer could still see as real stock. Only `'delisted'`
  (not live on eBay at all) is excluded now.
- `push_prices()` no longer skips rows whose resolved price/qty match
  what we last recorded as pushed (`pushed_price`/`pushed_qty`) — it
  now always resends every synced, live-status row's current true
  value on every push. That diff-against-our-own-history logic is
  exactly what let the 51-row corruption go undetected: our own
  records said those rows were already correct, so nothing ever
  re-checked them against eBay's actual live state. Always overwriting
  with current truth is self-healing against drift from *any* cause,
  not just this specific bug.

Both listings (`336691613250` "Reverse Holo - Ultra Rare" and
`336691917730` "Common") were fully re-pushed with the fix in place and
verified clean: every live eBay quantity checked against what was
intended, zero mismatches on both (124/124 and 84/84 rows respectively).

### Separate bug: pushing a new variation at 0 available quantity silently no-ops (2026-08-01)
Found while Fei was testing add/remove-variation flows. Pushed a queued
card (`Charcadet`, a promo) live via `push_single_card_live()` while it
had 0 available inventory — the call returned success, `platform_listings`
recorded it as `active` and pushed, but the variation was never actually
live on eBay. Removing it then failed with "not found live — mismatch,
needs manual reconcile in Seller Hub", since there was nothing there to
remove.

Root cause: `_post()` (`importer/ebay.py`) only raises on
`Ack=Failure`; eBay returned `Ack=Warning` with two messages our code
never inspects: "SKU missing in variation" and, critically,
**"Variations with quantity '0' will be removed."** — eBay silently
drops any variation added at quantity 0, no hard error, just a warning
our code discarded. Confirmed by bypassing `_post()`'s Ack check and
printing the raw `<Errors>` block directly against a real revise call.

Fix: `push_single_card_live()` now checks `promoted_resolved
["available_qty"]` up front and refuses with a clear error before
attempting anything, if it's 0 — `_stage_promotion()`'s downstream
low_stock/quantity_limit math only ever narrows qty_to_push further, so
0 available_qty can never produce a nonzero push regardless. Added the
same guard to both promotion paths in `_do_promotions()` (250-cap
auto-promotion) — a 0-qty queued card is now skipped (left queued for
a future run) rather than "successfully" promoted into a phantom
platform_listings row. Verified live: the same Charcadet push now
fails immediately with a clear message, no DB writes, roster row stays
cleanly `queued`.

## PLANNING (2026-08-02, session 14) — genuinely new listings, spreadsheet
## import, per-copy photo library. Nothing in this section is built yet;
## all three items below were confirmed with Fei in conversation before
## any code/schema work starts.

### 1. Create a genuinely new eBay listing (not just add to an existing one)
Everything built so far — `push_prices()`, `push_single_card_live()`,
`_do_promotions()` — only ever **revises** a listing that already exists
on eBay (`ReviseItem`/`ReviseFixedPriceItem`). Confirmed via a repo-wide
grep: zero references to `AddFixedPriceItem`/`AddItem`/`CategoryID`/
`ListingDuration`/business-policy fields anywhere. `listing_templates`
only ever stored pricing/naming config for variations added to a listing
that already existed — it has no columns for the listing-level metadata
(category, description, item location, duration, shipping/return/payment
policy) that `AddFixedPriceItem` requires, because nothing has ever had
to submit that metadata before.

**Confirmed with Fei:**
- Listing-level metadata can come from **either** cloning an existing
  listing **or** manual entry, chosen per new listing (not an either/or
  architecture decision — both paths need to exist, manual is explicitly
  the fallback "for when the type of listing doesn't already exist to
  clone from").
- Fei's eBay account uses **Business Policies** (shipping/return/payment
  are account-level named profiles referenced by ID, not raw per-listing
  blocks). Cloning copies the profile IDs straight off the source
  listing's `GetItem` response. Manual entry needs a profile **picker**
  (sourced from `GetUserPreferences` /
  `ShowSellerProfilePreferences=true`, which lists the account's
  configured profiles), not free-text policy entry.
- First push is a **batch**, not a one-card bootstrap: "Create listing"
  submits every *ready* queued roster row as the initial `<Variations>`
  set in one `AddFixedPriceItem` call, rather than creating the listing
  with one card and adding the rest through the existing add-variation
  path afterward.
- Partial readiness is allowed and expected: dry-run first (matches the
  existing Push button's confirm-dialog convention), show which queued
  rows are ready vs. excluded and why (no resolved price, 0 available
  qty, etc.), let Fei choose to push just the ready subset. Excluded rows
  stay `queued` — once the listing has a real `listing_id`, they go live
  later through the **already-built** `push_single_card_live()` /
  250-cap-promotion paths unchanged. No new "top-up" mechanism needed —
  this feature only replaces the bootstrap (first-ever) push for a
  listing that doesn't exist yet.

**New schema** — `listing_templates` gains (only populated while
`listing_id` is still blank, i.e. before the first successful create
push):
- `source` (`'cloned'` | `'manual'`)
- `cloned_from_listing_id` (nullable, the eBay ItemID cloned from)
- `category_id`
- `title`
- `description_html`
- `item_location`
- `listing_duration`
- `payment_policy_id` / `return_policy_id` / `shipping_policy_id`
  (Business Policy profile IDs)

**New Trading API surface needed:**
- Extend the existing `GetItem` call/parsing in `importer/ebay.py`
  (already used for listings-import) to also pull `CategoryID`,
  `ItemLocation` (`Location`/`PostalCode`/`Country`), `ListingDuration`,
  `Description`, and `SellerProfiles` (the Business Policy IDs) — today
  it only extracts variation/title data.
- New `GetUserPreferences` call (`ShowSellerProfilePreferences=true`) to
  list the account's configured shipping/return/payment profiles for the
  manual-entry picker.
- New `AddFixedPriceItem` call: builds a fresh `<Item>` — the listing-
  level XML (category/description/location/duration/`SellerProfiles`)
  plus a `<Variations>` block built from every ready queued row, reusing
  the existing `add_variation_row`/naming/picture helpers in
  `ebay_variations_xml.py`. Returns the new `ItemID`.

**DB writes only after a successful `AddFixedPriceItem`** (same
"never write before a confirmed eBay success" convention already used by
`_do_promotions()`):
- `listing_templates.listing_id` = the returned `ItemID`
- one `platform_listings` row per included card (`status='active'`,
  `pushed_price`/`pushed_qty`/`pushed_at` set — same pattern as the
  existing promotion `INSERT`)
- `listing_card_assignments.status='active'` + `platform_listing_id` per
  included card
- `ebay_listing_map` upsert per included card (same as `_stage_promotion`
  already does for existing-listing promotions)

**Still open, to resolve once this is actually picked up:** the exact
full field list `AddFixedPriceItem` needs beyond what's listed above
(`ConditionID`, item specifics beyond `VariationSpecifics`, payment
method assumptions) — needs checking against a real `GetItem` response
from one of Fei's live listings while building, not guessed at now. Also
where "Create new listing" lives in the UI — likely extends the existing
"offer to create a template if none exists for the typed Item #" flow on
the Listing pricing page, replacing the Item#-required assumption with a
clone-vs-manual choice up front.

### 2. Excel-to-staging importer (separate feature — build independently,
### not in the same pass as new-listing-creation, per Fei's explicit call)
New cards do **not** get created from inside the Listing Pricing System
at all. Instead: import a spreadsheet directly into the existing
`staging` table, through the same review/approve pipeline every other
importer already uses. Once a spreadsheet-sourced card clears
`--approve` into real inventory, it's just an existing card as far as
listing creation is concerned — it's found through the
**already-built** "Add card to listing" search on the Listing pricing
page. This removes the need for any new-card-creation UI inside the
listing feature itself.

**Confirmed with Fei:**
- Matching is **automatic** — set + card number against our own catalog,
  not a separate manual mapping table Fei maintains by hand.
- If a row doesn't match locally, it falls back to a live PokemonTCG API
  search (revised after initial build — see build log below) before
  ever resorting to manual creation.
- Wrong auto-matches get fixed by hand afterward, reusing the existing
  `--review` flow's card-match fixup (the same tool already used for
  ambiguous matches today) — no new correction UI needed.

### Built (2026-08-02/04)
`importer/excel_staging.py` + `--excel-staging PATH [--dry-run]` in
`main.py`, plus a generated sample workbook at
`docs/plans/card_import_template.xlsx` (headers + a Field Guide sheet —
`docs/plans/build_card_import_template.py` is the one-off generator
script). Column layout: `card_name`, `set_name`, `set_code` (only needed
for a genuinely new set), `card_number`, `rarity`, the seven variant
axes, `is_promo`/`is_first_edition`, `image_url`, `condition`,
`quantity`, `price`, `purchase_date`, `source`, `reference_id`, `notes`.

**Resolution order ended up three-tier, not two** (Fei revised this
after the first version): local `card_master` match by set + card
number first (no network call) → PokemonTCG API search for anything not
found locally, fired **in parallel** across every such row in one
import (`ThreadPoolExecutor(max_workers=15)`, same pattern as
`market_price_refresh.py` — only the HTTP searches run concurrently,
the DB writes that finalize each match happen sequentially afterward to
avoid any concurrent-insert race on `get_or_create_set`) → manual
creation straight from the row's own spreadsheet columns only as the
final fallback, when neither the local catalog nor the API has the
card. A row matching more than one candidate (locally or via the API)
is left `ambiguous` for `--review` — extended
`_resolve_ambiguous()` in `importer/staging_workflow.py` to handle
locally-sourced candidates (tagged `card_id`) alongside its existing
API-search-result candidates, so a local duplicate-match collision can
be resolved without an API call.

Also fixed two real bugs surfaced while building/testing this against
live data:
- `db/staging.py`'s `get_staging_rows()` and `importer/image_upload.py`
  both still selected the long-dropped `card_master.variant` column —
  **`--review` itself was completely broken**, not just
  `--upload-image` as first suspected. Confirmed live (ran the exact
  query, got `column cm.variant does not exist`) before fixing both.
- `utils/pokemon_api.py`'s retry-message `print()`s used emoji that
  crash on Windows' cp1252 console — only surfaced once this importer's
  parallel API path actually exercised a live retry for the first time
  in this environment.

Verified via dry-runs against live data (no writes): local match, API
fallback + create-from-API, and manual-fallback-after-both-fail all
confirmed correct on the real template plus a throwaway edge-case
workbook (missing fields, bad condition, non-numeric qty/price,
unresolvable set) — all skip cleanly with a clear reason, no crash.

**Not yet done**: Fei hasn't run this against a real filled-out
spreadsheet yet (only the sample template + synthetic edge cases so
far) — worth a real pass before trusting it for a bulk import.

### 3. Per-copy photo library (header/detail), separate from
### `card_master.image_url_own`
**Problem:** `card_master.image_url_own` is a single, app-wide "own
photo" per card — built as `--upload-image`
(`importer/image_upload.py`), CLI-only (interactive search + local file
path), **and currently broken**: its card-lookup queries still select
`card_master.variant`, a column dropped when the seven-axis
`card_variants` model replaced it (per `find_card_by_name_set`'s own
comment in `db/connection.py`) — running `--upload-image` today errors
on that missing column. Separately, `listing_card_assignments.
eps_picture_url` (built session 6) already supports one eBay-hosted
picture per queued roster row.

Neither fits what Fei actually needs: pricy cards get photographed front
**and** back, and Fei often holds multiple physical copies of the same
`card_id` at once, split across different listings (the existing Balance
Qty feature already assumes this is common) — each copy needs its own
distinct photo set, which a single `image_url_own` column or a single
`eps_picture_url` string can't represent.

**Confirmed design (header/detail, Fei's explicit preference over a flat
ordered list of photos):**
- `card_photos` (header — one row per physical copy's photo set): `id`,
  `card_id`, `front_eps_url`, `label` (free text so Fei can tell copies
  apart, e.g. "Copy A — NM"), optional `has_additional` flag (redundant
  with detail-row existence — zero detail rows already means
  single-image — kept anyway per Fei's ask), `created_at`.
- `card_photo_details`: `id`, `card_photo_id` (FK to the header), `eps_url`,
  `label`, `sort_order` — one row per photo beyond the front (back,
  close-up, etc.).
- Deliberately separate from `card_master.image_url_own`, which keeps
  acting as the generic fallback shown wherever no listing-specific group
  is staged (same fallback behavior the current single-`eps_picture_url`
  mechanism already has).
- Staging a queued roster row's picture(s) becomes: pick one
  `card_photos` group for that card (not individual photos) — the front
  plus every `card_photo_details` row, in `sort_order`, gets staged/
  pushed together for that row.

**Real gap found while designing this:** `set_variation_picture()`
(`ebay_variations_xml.py`) only ever writes **one** `<PictureURL>` per
variation today (finds-or-creates a single entry, overwrites it) — eBay's
Trading API natively supports multiple `<PictureURL>` entries per
`VariationSpecificPictureSet`, this codebase just never exercised it
since no feature needed more than one picture per variation before now.
Needs extending to accept an ordered list of URLs and write all of them,
or front+back never actually reaches eBay. `listing_card_assignments.
eps_picture_url` (currently one raw URL column) also needs to become a
reference to a `card_photos` group instead, so a row can carry more than
one staged picture.

**Not yet designed:** the upload flow for building a new group (upload
front, optionally add more via `card_photo_details`, label it) —
presumably a new modal replacing/extending the current single-URL/
single-file "stage picture" modal.

### Known bug surfaced during this planning conversation — fixed same session
`importer/image_upload.py` (`--upload-image`) and `db/staging.py`'s
`get_staging_rows()` (i.e. **`--review` itself, not just
`--upload-image`**) both queried the long-dropped `card_master.variant`
column — confirmed live (`column cm.variant does not exist`) before
fixing both by dropping the dead references. Verified both queries run
clean afterward.

## BUILT (2026-08, session 15) — item 1: create a genuinely new eBay listing
Backend only, per Fei's call (this session's repo access is CBMM-only —
the web UI is a separate session with `card-board-mastermind-WebInvManagement`
open). Everything below is real, tested against live data (dry-runs and
read-only calls only — no `AddFixedPriceItem` has actually been sent for
real yet, deliberately, since that publishes a real live listing).

**Migrations 016-018** (`docs/plans/listing_pricing_migration_01{6,7,8}_*.sql`):
- 016: `resolve_listing_prices()` gains an optional `p_template_id uuid`
  parameter — looks up the template by `id` when provided, by
  `(platform, listing_id)` otherwise (fully backward compatible,
  verified live: old 2-arg call and new template_id call return
  byte-identical row counts/prices for the same template). Fixes the
  actual blocker (`WHERE listing_id = p_listing_id` can never match a
  NULL `p_listing_id`) plus a real correctness bug it exposed: the
  "claimed by other listings" quantity subtraction used to silently
  count nothing when `p_listing_id` was NULL (three-valued NULL logic),
  which would have over-promised quantity on a new listing's first
  push — fixed with `(p_listing_id IS NULL OR pl2.listing_id <>
  p_listing_id)`, verified live via a real variant whose stock is
  already fully committed to an existing listing (correctly resolves to
  0 available for a hypothetical new listing, not the full pool).
- 016 also adds `listing_templates.source` / `cloned_from_listing_id` /
  `category_id` / `title` / `description_html` / `item_location` /
  `item_country` / `listing_duration` / `payment_policy_id` /
  `return_policy_id` / `shipping_policy_id`. 017 and 018 are small
  same-day follow-ups for two fields found missing only once a real
  `GetItem` response was inspected: `item_postal_code` (distinct from
  `item_location`, needed for shipping calc) and `condition_id`.

**Every eBay field name was confirmed against a real live response
before being used** — pulled an actual `GetItem` (item `336204674240`)
and `GetUserPreferences` (`ShowSellerProfilePreferences=true`) response
and inspected them directly rather than guessing from eBay's docs.
Concrete findings that shaped the build: `SellerProfiles/
SellerPaymentProfile/PaymentProfileID` (+ Return/Shipping siblings) is
how Business Policies actually appear on a real item; `PrimaryCategory/
CategoryID`; `ConditionID` (`4000` = "Ungraded" for this account's
cards); a real variation listing still carries a top-level `Quantity`/
`StartPrice` even though the real values live per-variation; this
account's `VariationSpecificsSet` name is consistently `"PokeCards"`
across every existing listing (a discovered convention, not a hard
requirement — defaulted, not hardcoded, in the new code); `GetUserPreferences`'
real shape is `SellerProfilePreferences/SupportedSellerProfiles/
SupportedSellerProfile` with `ProfileID`/`ProfileType` (`PAYMENT`/
`RETURN_POLICY`/`SHIPPING`)/`ProfileName` — not the
`SupportedSellerProfiles`-flat shape assumed before checking.

**New module `importer/ebay_create_listing.py`:**
- `list_business_policies(account_num)` — `GetUserPreferences` wrapper
  for the manual-entry picker.
- `fetch_listing_metadata(item_id, account_num)` /
  `clone_listing_metadata(template_id, source_listing_id, account_num)` —
  `GetItem`-based clone, snapshots onto the template row (not re-fetched
  live at create time).
- `set_manual_listing_metadata(template_id, **fields)` — same columns,
  hand-entered, partial updates allowed.
- `preview_new_listing(template_id, account_num)` — dry-run-first
  readiness check (ready vs. not-ready-and-why, missing metadata),
  pure DB read via migration 016's `p_template_id` path.
- `create_listing(template_id, account_num, dry_run, specific_name)` —
  the actual batch `AddFixedPriceItem`. Builds one `<Item>` with a fresh
  `<Variations>` block (reusing `add_variation_row`/
  `insert_specifics_value`/`set_variation_picture` from
  `ebay_variations_xml.py`, and `_render_variation_name` from
  `ebay_listing_sync.py` — no new naming logic). 0-qty queued rows are
  excluded and stay queued, same "skip, don't block the batch" rule
  `_do_promotions()` already uses. DB (`listing_templates.listing_id`,
  `platform_listings`, `listing_card_assignments.status`,
  `ebay_listing_map`) is only written after a confirmed successful
  `AddFixedPriceItem` response — same rule every other push in this
  feature already follows, now applied to a first-ever create instead
  of a revise.

**Real bug caught via self-testing, fixed same session**: `create_listing()`'s
`--dry-run` path returns the full built request XML for inspection —
the first push in this feature whose dry-run output includes the raw
request rather than just a summary. That XML embeds the live Auth'n'Auth
token (needed for a real send), and the token appeared in plaintext in
this session's own transcript during testing. Fixed: `dry_run=True` now
substitutes a placeholder token string instead of calling
`get_user_token()` at all, so a dry-run can never leak a real
credential regardless of where its output ends up (log, future web UI,
pasted transcript). **Fei was advised to rotate that token as a
precaution** since the real one was genuinely exposed once before the
fix landed.

**CLI** (`main.py`): `--ebay-list-policies`, `--ebay-clone-listing-metadata
--template-id X --from-listing-id Y`, `--ebay-set-listing-metadata
--template-id X --metadata-json '{...}'`, `--ebay-preview-new-listing
--template-id X`, `--ebay-create-listing --template-id X [--dry-run]`.

**API** (`picking_api.py`, for the future web UI): `GET
/api/business-policies`, `POST /api/listing-metadata/clone`, `POST
/api/listing-metadata/manual`, `GET
/api/preview-new-listing/{template_id}`, `POST /api/create-listing` (own
lock, same auth as every other endpoint in the file).

**Verified live** (throwaway test template + one real roster row,
deleted after each test — no residue): `--ebay-list-policies` (real
read), `--ebay-clone-listing-metadata` (real `GetItem` read + DB write,
all 11 fields populated correctly), `--ebay-preview-new-listing` (both
the "0 available, already claimed elsewhere" and "1 available, ready"
cases), `--ebay-create-listing --dry-run` (built a complete, correctly-
escaped `AddFixedPriceItem` request against real cloned metadata + a
real ready card — HTML description's `&`/`<`/`>` escaped correctly via
ElementTree text-node serialization, token redacted).

**Not yet done**: an actual live `AddFixedPriceItem` send (deliberately
not attempted — publishes a real listing, Fei's call when ready);
category-specific Item Specifics beyond `VariationSpecifics` (still
unknown whether any of Fei's real categories require them — will only
surface as a real eBay error on first live attempt, surfaced via
`_post()`'s existing error-raising, not guessed at now).

### Web UI built same session — turns out this session had access after all
Despite the "separate session" note above, this session's directory
access unexpectedly reached `card-board-mastermind-WebInvManagement` too
(confirmed via `ls`/`Glob`/`Read`/a real write test before trusting it) —
so the web UI landed in the same session instead of needing a handoff.

`listing-pricing.js`: opening a template with no `listing_id` yet no
longer refuses with an alert — it's a genuine "draft" state now.
`state.templateId` (set by `openTemplate()`) is the lookup key going
forward instead of `(platform, listing_id)`, which can never match a
NULL `listing_id` (same root issue migration 016 fixed on the RPC side).
Draft templates get a "Listing metadata" panel in place of the Push/
Dry-run buttons: clone from an existing listing (`GetItem` via the new
`/api/listing-metadata/clone`), or edit every field by hand with real
payment/return/shipping dropdowns sourced live from
`/api/business-policies`; "Preview readiness" and "Create listing"
(dry-run-confirm, same UX pattern as the existing Push button) call the
matching new endpoints. Per-row "Push live" is hidden for draft rows —
it revises an already-live listing, which doesn't exist yet. Existing
(non-draft) templates take the exact same code path as before, unchanged.

Verified via brace/paren/backtick balance check + manual re-read only —
no JS runtime in this environment, same standing limitation as every
prior UI pass on this feature; needs a real browser pass before trusting
it fully.

### Excel import gets a web front end (2026-08, session 16)
Fei asked for a front end for `--excel-staging` rather than CLI-only.
Built as a third job type on the existing Jobs page/`job_runner.py`
infrastructure (not a synchronous request) since resolution does live
PokemonTCG API calls per unmatched card and a big sheet could plausibly
run past a normal request timeout — same reasoning `market_price_refresh`
already established.

`import_from_excel()` gained an optional `job_id` param, reporting
progress through `update_job()` at natural phase boundaries (parsing →
api_lookup, with a live done/total count as each parallel API call
completes → writing → done with final totals) — CLI usage is unchanged,
`job_id` just defaults to `None`. New `POST /api/jobs/excel-import` in
`picking_api.py` (multipart upload, mirrors `/api/stage-card-picture-file`'s
pattern) saves to a temp file, starts the job, and cleans the temp file
up afterward regardless of outcome. `jobs.js` gained a third "start" box
(file input + dry-run checkbox, checked by default) and a
`jobProgressText()` case for `excel_import`'s progress/result shape —
the existing Recent Jobs table and polling needed no changes at all,
exactly as `job_runner.py`'s own docstring promises for a new job type.

### Revise metadata on an already-live listing (2026-08, session 16)
Fei's follow-up: the clone/manual metadata flow only ever covered a
listing at creation time — once a draft's `create_listing()` succeeds
and `listing_id` is set, there was no way to touch title/description/
category/policies/etc. again. New `revise_listing_metadata()`
(`ebay_create_listing.py`) — the mirror image of the create-time step,
via `ReviseFixedPriceItem` instead of `AddFixedPriceItem`. Only
top-level `<Item>` fields are touched, no `<Variations>` at all — unlike
the variations block, eBay only requires sending the fields you're
actually changing for a plain top-level revise, confirmed by reusing
the same partial-revise pattern `push_prices()`'s single-listing path
already established. `fields` is merged with the template's current DB
values so every call is a complete, consistent request even from a
partial diff — verified live via `--dry-run` against a real listing
(`336204674240`/template `01d68e72-...`): correctly sent only `<Title>`
alongside `<ItemID>`, leaving every other real eBay field untouched
rather than blanking anything not yet captured locally.

Not everything is necessarily revisable once a listing has real
activity — eBay commonly restricts `ListingDuration`/`ConditionID`
post-listing — deliberately not pre-validated; a rejection surfaces as
a normal eBay error via `_post()`, same as the still-open
category-specific-Item-Specifics unknown from the create flow.

CLI: `--ebay-revise-listing-metadata --template-id X --metadata-json
'{...}' [--dry-run]`. API: `POST /api/listing-metadata/revise` (own
lock). Web UI: the "Listing metadata" panel (previously draft-only) now
also shows for already-live templates, with just an "Edit fields"
button (no clone/preview/create — not applicable once live) —
`metadataPanelHTML(isDraft)`/`wireMetadataControls(...)` replace the
earlier draft-only versions. The shared "Edit fields" modal
(`openManualMetadataModal`, now takes `isDraft`) works identically
either way since it always pre-fills from whatever's currently on the
template — cloned, manually set, or already-live — but its Save
button branches: DB-only write for a draft, or a dry-run-then-confirm-
then-real revise for a live listing (same UX pattern as the Push
button), with an explicit warning in the modal that saving on a live
template is a real eBay write, not just a local edit.

### SKU, Card Condition, and Item Specifics (2026-08, session 17)
Fei's follow-up from real eBay Sell-form screenshots surfaced three more
real fields, none guessed — grounded via a live GetItem on a second real
listing (`336691613250`) plus eBay's own condition-descriptor docs:

- **SKU** — confirmed on `336691613250` ('VarSinglesHolo') as a genuine
  top-level `<Item><SKU>`, a single free-text "Custom label" for the
  whole listing (not per-variation — every real `<Variation>` on that
  listing has no SKU of its own, contrary to the first assumption).
- **Card Condition** (Near Mint/Lightly Played/etc.) is a field entirely
  separate from `condition_id` — `ConditionDescriptors/
  ConditionDescriptor`, `Name=40001` (fixed constant,
  `CONDITION_DESCRIPTOR_NAME`) with a numeric `Value`. Full code table
  for category 183454 ("Collectible Card Game", Fei's real category)
  found via eBay's official docs: `400010`=Near mint or better,
  `400015`=Lightly played (Excellent), `400016`=Moderately played (Very
  good), `400017`=Heavily played (Poor) — a different category could use
  different codes, not handled. Also confirmed `condition_id` itself is
  `4000` (Ungraded) or `2750` (Graded) — cleaned up from a raw-text
  input into an actual 2-option dropdown now that both values are known.
- **Item Specifics** — confirmed live: `ItemSpecifics` is an arbitrary
  `NameValueList` bag (Game, Set, Language, Manufacturer, Year
  Manufactured, Card Type, Country/Region of Manufacture, Country of
  Origin on the real listing checked), plus Fei's ask for a free-text
  `Character` field ("an array of characters... I don't use it yet").
  Stored as one `item_specifics jsonb` column rather than one column per
  specific — the set isn't closed/fixed, matches eBay's own shape
  directly, and avoids a migration every time a new specific comes up.

**Migration 019**: `listing_templates.sku`, `.condition_descriptor_value`,
`.item_specifics jsonb default '{}'`. All three added to a new
`OPTIONAL_METADATA_FIELDS` list (distinct from `REQUIRED_METADATA_FIELDS`,
which still alone gates "Create listing" readiness) — category-specific/
not universally required by eBay, so they're included in a request when
set but never block create/revise readiness the way title/category/
policies etc. do. `ALL_METADATA_FIELDS = REQUIRED + OPTIONAL` is what
`set_manual_listing_metadata()`/`revise_listing_metadata()` actually
accept.

`fetch_listing_metadata()` (clone path), `_build_add_item_xml()`
(create), and `revise_listing_metadata()`'s XML builder all extended
identically — `<SKU>`, `<ConditionDescriptors>` (only emitted when a
value is set), `<ItemSpecifics>` (only emitted when the dict is
non-empty, skips any blank values). Verified live via `--dry-run`:
correctly rendered `<SKU>`, `<ConditionDescriptors><ConditionDescriptor>
<Name>40001</Name><Value>400010</Value></...>`, and two `<ItemSpecifics>
<NameValueList>` entries against a real listing/template.

Web UI: Country/Postal code reverted from the earlier dropdown-of-
prior-values back to plain text per Fei's explicit ask (only Category,
Duration, Location stayed as dropdown-of-prior-values). Condition type
and Card condition are now real fixed dropdowns (2 and 4 options
respectively, matching eBay's own Sell-form choices) instead of a raw
ID text box. Item Specifics render as one labeled text input per known
name (`ITEM_SPECIFICS_FIELDS`), assembled into one `item_specifics`
object on save; blank inputs are simply omitted rather than written as
empty strings.

### Roadmap — not started
- ~~**Proper description composition.**~~ Done (8/09) — built as a full
  customizable description module builder (migration 029): every block
  (static HTML, a repeater over related listings, or a single block) is
  a named `description_sections` row with its own repeat_rule/layout/
  item-template markup, dispatched generically by key instead of 4
  hardcoded token functions. `listing-pricing.js`'s description editor
  is now a reorderable block list (pick a module or add free text,
  reorder/remove) with an "Advanced (raw HTML)" escape hatch, not a raw
  textarea. `description_html` itself is unchanged in storage — still a
  plain `{{key}}`-containing string, the builder just constructs/parses
  it. See `importer/ebay_descriptions.py`'s module-dispatch section and
  `docs/plans/listing_pricing_migration_029_description_modules.sql`.
- **Description-nav milestone 4 (sanitizer test).** Backend + frontend for
  the description navigation system (family strip / era hub-and-spoke /
  era index — migration 020, `importer/ebay_descriptions.py`,
  `picking_api.py` description-preview/-sync/-presets endpoints,
  `listing-pricing.js` nav fields + preview/push UI) are built and, on the
  Python side, verified against real DB state. NOT yet done: picking one
  real live listing, pushing a real test description to it via
  `ReviseFixedPriceItem`, and comparing what eBay's sanitizer actually
  keeps vs. strips — the plan's block markup (inline styles only, table-
  friendly, no JS/external CSS) is deliberately not finalized until this
  runs. Deferred — needs Fei to pick the sacrificial listing.
- **Description-nav milestone 9 (rollout).** Assigning `set_id` /
  `finish_kind` / `family_label` / `nav_rank` / `is_set_primary` across
  Fei's real templates, running `--ebay-backfill-nav-images`, adding nav
  tokens family by family, and pushing era by era via
  `--ebay-sync-descriptions`. Blocked behind milestone 4 (block markup
  isn't finalized yet) — deferred until then.
- **`--ebay-backfill-nav-images` variation-photo fix.** Confirmed real
  (8/08): both Pitch Black templates (Common + Reverse Holo - Ultra Rare,
  distinct `listing_id`s) got the identical `nav_image_url`. Root cause:
  the backfill pulls `PictureDetails/PictureURL` — the listing's top-level
  gallery photo — which for a variation listing is typically a generic
  cover/box-art photo, not per-card; if the same cover image was uploaded
  to both listings, eBay legitimately returns the same hosted URL for
  both. Fix would mean pulling a representative photo from
  `Variations/Variation/VariationSpecificPictureSet` instead of the
  top-level `PictureDetails`. Deferred — manual `nav_image_url` overrides
  work fine as a stopgap in the meantime.
- ~~**`description_sections` section library.**~~ Done (8/08-8/09) —
  built, then superseded in place by the module-builder redesign above
  rather than left as a separate `'layout'`/`'section'`/`'item_template'`
  kind split. Deep Sea dark theme (navy/cyan palette, `bgcolor` + inline
  `style` sanitizer hardening) shipped as the seeded default look, with
  per-shop/listing-group theme scoping (`description_theme_settings.
  theme_key`, migration 028) on top.

### Generic advanced card search + batch-add-to-roster (2026-08-16, session 18)

Fei wanted a brand-new "Mega Evolution ex" listing template (all 65
"Mega ___ ex" cards / 76 `card_variants` rows across the 7 Mega Evolution-
series sets) without hand-picking each one through the existing
"Add card to listing" search-one-click-one flow. Explicitly asked for the
mechanism to be generic/reusable — era, set, Pokémon name, evolution line,
rarity, all seven `card_variants` axes, and "other Pokémon shown in a
card's art" as filter dimensions for future themed listings too.

Built `search_roster_candidates(p_template_id, p_series, p_set_ids,
p_name_terms, p_related_pokemon, p_rarities, p_foil_types,
p_foil_patterns, p_textures, p_materials, p_sizes, p_stamp_types,
p_source_types, p_exclude_secret_rare)` (migration 032) — same
single-source-of-truth convention as `resolve_listing_prices()`. Every
array param is NULL-means-unconstrained. `is_secret_rare` is computed as
`card_number_numeric > card_sets.total_cards` (false when `total_cards`
is NULL — a handful of promo sets have none, treated as "can't
determine" rather than excluded). Always excludes variant rows already on
the target template's roster in any status, moving `importExisting()`'s
client-side dedup server-side so every future caller gets it for free.

`listing-pricing.js` gained a self-contained "advanced card search"
function group (`loadAdvancedSearchOptions`/`advancedCardSearchFilterHTML`/
`collectAdvancedSearchFilters`/`runAdvancedCardSearch`, `acs-`-prefixed
element ids) — deliberately NOT hard-wired only into the new
"+ Batch add cards" button/`openBatchAddModal()`, per Fei's explicit ask
that this be reusable elsewhere later, not a one-off. Filter panel only
ever offers axis/rarity/set values actually in use (queried from
`card_variants`/`card_master` directly, not the full lookup tables), so a
filter option can never silently return zero rows.

**Evolution line**: initially assessed as "no data, deferred" (this repo's
own `card_master`/`card_attributes` have no `evolvesFrom`), which was
wrong — corrected after Fei pointed out this DB separately carries a full
PokeAPI-style species mirror (`pokemon`, `pokemon_evolution_chains`,
`pokemon_evolutions`, `pokemon_names`, seeded by the standalone
`load_pokemon.py`/`load_pokemon_forms.py` scripts, never previously joined
to `card_master` anywhere in the codebase). The evolution-line picker
resolves a chosen species' `evolution_chain_id` via `pokemon_evolutions`
to get every base stage, THEN separately fetches every `pokemon` row whose
`base_pokemon_id` points at one of those base stages (Mega/regional/Gmax
alt forms) — Fei's explicit call was that alt forms show up as their own
individually toggleable chips, not silently folded into the base name's
substring match. Both feed the same `p_name_terms` OR'd-substring list the
plain Name field builds — no special-case RPC path needed.

**"Related Pokémon" / other Pokémon shown in a card's art**: `card_master.
scenes` is a real column but 100% empty with zero code references
anywhere in CBMM — nothing has ever written to it. Fei confirmed he'll
backfill it manually himself eventually, so the filter control (RPC
param `p_related_pokemon`, `cm.scenes && p_related_pokemon`, plus a
"Related Pokémon" text field in the UI) shipped now even though it
matches nothing today — nothing else needs to change once `scenes` gets
real data.

Lesson worth remembering: don't declare a filter/feature dimension
"no backing data" from checking only the obviously-named tables
(`card_master`/`card_attributes` here) — this schema has standalone
reference-data tables (a whole PokeAPI mirror, in this case) that aren't
joined to the main card tables anywhere in existing code, so a `grep` for
existing usage returning nothing does NOT mean the data doesn't exist
elsewhere in the schema. Check `information_schema.tables` for
adjacently-named tables before ruling a feature out as a data gap.

**Bug found by Fei immediately after first use**: the Rarity filter was
missing Double Rare/Ultra Rare and others. Root cause: `card_master`
(5,823 rows) and `card_variants` (9,443 rows) both exceed Supabase/
PostgREST's default 1,000-row REST response cap — `loadAdvancedSearchOptions()`
had been fetching the whole rarity/axis column and deduping client-side,
which silently truncated to whichever 1,000 rows came back first (no
`ORDER BY`), so any value only present later in the table — Double
Rare/Ultra Rare, common on the newer Mega Evolution-era cards — never
showed up. Same cap risk existed in `search_roster_candidates()` itself:
an unfiltered/broad call matches up to 9,422 rows today, so the "Batch
add cards" preview could have silently shown a truncated, arbitrary-order
subset with no indication anything was cut off. Fixed both: migration 033
adds `advanced_search_filter_options()` (one RPC, `DISTINCT` computed
server-side, replaces the truncatable column fetches); migration 034
adds an explicit `p_limit` (default 500) to `search_roster_candidates()`
so it caps itself deterministically instead of relying on the invisible
REST cap, and the JS treats "got exactly 500 back" as "there may be more
— narrow your filters" rather than silently presenting it as complete.
**Lesson**: any Supabase-client `.from(table).select(...)` against a
table that could plausibly exceed ~1,000 rows needs either an explicit
`.range()`/pagination loop, a server-side `DISTINCT`/aggregate via RPC, or
an explicit `.limit()` the caller is aware of — the default cap fails
silently, not with an error, so it's easy to ship code that "works" in
testing against small result sets and quietly drops data once the table
grows past it.

**`display_sort` made real (2026-08-16, same session)**: Fei noticed his
"ME-EX" draft template had `display_sort = 'alpha'` set but the eBay
variation dropdown wouldn't actually come out alphabetical. Root cause:
`resolve_listing_prices()` had a hardcoded `ORDER BY set_name,
card_number_numeric`, never consulting `display_sort` at all — and
`_compute_insert_position()` (`ebay_listing_sync.py`, used when promoting
one more card into an already-live listing) explicitly only implemented
`'card_number'`, falling back to append-at-end for anything else, per its
own docstring. So `'alpha'`/`'release_date'` were selectable in the web
UI but did nothing anywhere. Asked Fei which extra modes to add; he asked
for all of: `card_number` (today's actual default, grouped by set),
`number` (NEW — card number only, ignores set), `alpha`, `release_date`,
`rarity` (NEW). Built:
- `rarities` lookup table (migration 035) — same `code`/`display_name`/
  `sort_order` shape as `foil_types`/`textures`/etc. (none existed for
  rarity before). Seeded with the 15 live `card_master.rarity` values in
  a best-effort standard-TCG tier order — explicitly NOT confirmed
  value-by-value with Fei, trivially fixable via a plain `UPDATE` per row
  if the order's wrong, no re-migration needed. Unmapped future rarities
  sort last via `COALESCE(..., 999999)`, not first/error.
- `resolve_listing_prices()` (migration 036) — added `release_year` +
  `rarity_sort_order` to its internal CTEs, fetches the template's own
  `display_sort` into `v_display_sort`, and replaced the hardcoded
  ORDER BY with conditional CASE-key columns (each mode's keys evaluate
  to NULL — a no-op — unless `v_display_sort` matches; original
  `set_name, card_number_numeric` stays as the unconditional final
  tiebreaker for `card_number`/unrecognized values). No dynamic SQL/
  `EXECUTE` needed. Caught during verification: "Mega Evolution" and
  "Phantasmal Flames" share `release_year = 2025`, so `release_date`
  needed `set_name` as a tiebreaker between year and card number or
  same-year sets would interleave instead of staying grouped — fixed
  before this was ever committed.
- `_compute_insert_position()` rewritten around a shared
  `_insert_position_sort_key()` helper producing a comparable tuple per
  card for whichever mode is active, replacing the old single hardcoded
  `card_number_numeric` comparison. **Known, explicitly accepted
  limitation carried forward unchanged**: `card_number`/`number` still
  only compare card numbers, never set membership, at promotion-insert
  time — matching `card_number`'s set-grouped *creation-time* order here
  too would need tracking per-set block boundaries in the live
  `VariationSpecificsSet`, a materially bigger feature. Not a
  regression — this function only ever implemented that same
  approximation for `card_number` before this change.
- Web dropdown (`listing-pricing.js`, `openTemplateModal`) gets the 2 new
  options and an accurate `release_date` label (dropped the stale
  "reserved — future themed listings" qualifier).

Verified directly against Fei's real ME-EX template (`fc23614f-...`):
flipped `display_sort` through all 5 values and confirmed
`resolve_listing_prices()` returns visibly correct, distinct orderings
for each (then restored it to his original `'alpha'`). Separately ran
`_compute_insert_position()` end-to-end against a real live listing's
`ebay_listing_map` rows (item 335662210469) for all 5 modes — no errors,
sane index/append-at-end results throughout. Also fixed, same session:
`_render_variation_name()` was rendering a double space whenever
`{suffix}` was empty (the common case — only reverse-holo/pattern/stamp
cards get a suffix) because format strings like Fei's own
`{name} {suffix} {number}/{set_total}` have literal spaces around the
token regardless of whether it resolves to anything; now collapses
repeated whitespace via `re.sub(r"\s+", " ", rendered).strip()` before
returning, so no format string needs to special-case an empty token's
surrounding spaces.

Sanity-tested directly against live data before touching the UI: the
Mega Evolution era filter combo returns real rows with `is_secret_rare`
false throughout (matches the 0-secret-rares finding from manual
inspection), and rarity/foil-type combinators compose correctly. Also ran
a reversible end-to-end smoke test of the exact search-then-insert path
`openBatchAddModal()`'s Add button performs — scratch `listing_templates`
row, ran `search_roster_candidates`, inserted 3 of its results into
`listing_card_assignments`, re-ran the same RPC call and confirmed those
3 `variant_id`s were now correctly excluded (84 candidates → 81, exact
diff), then deleted the scratch rows. Confirms the roster-dedup `NOT
EXISTS` clause and the insert shape both work against real schema/FK
constraints, not just in isolation. What's genuinely NOT done: no browser
tool available in this session, so the actual UI was never clicked
through — creating the real "Mega Evolution ex" template and using
"+ Batch add cards" against it live is still Fei's next step, and worth a
first-use sanity look given the filter panel/evolution-picker/chip UI
itself was only reviewed by re-reading the code, not exercised.
