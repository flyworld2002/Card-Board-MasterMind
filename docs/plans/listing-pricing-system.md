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
