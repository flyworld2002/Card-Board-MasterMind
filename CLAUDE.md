# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python CLI tool ("Card-Board-MasterMind" / CBM) that tracks Pokemon card
inventory with FIFO cost accounting, backed by a Postgres (Supabase) database.
It imports purchases from TCGPlayer and eBay, manages a review/approve staging
pipeline before anything hits real inventory, computes list prices, and syncs
eBay sales/fees back into the DB. There is no frontend in this repo — a
separate SPA (mentioned in code comments as "the Picking tab") consumes the
same Supabase database directly; `picking_api.py` exists only to let that
frontend trigger a live eBay pull from the browser.

Everything is invoked through `main.py`; there is no package structure beyond
top-level `importer/`, `db/`, and `utils/` folders imported by convention.

## Commands

```bash
pip install -r requirements.txt
```

There is no test suite, linter, or build step configured in this repo —
don't invent `pytest`/`ruff` invocations. Validate changes with `--dry-run`
where a command supports it.

Always invoke `python3` explicitly, not `python`, when running commands in
this repo.

Run `python main.py --help` (or read the module docstring at the top of
`main.py`) for the full, current flag list — it is the source of truth over
`README.md`, which documents an older CLI shape (`--tcgplayer orders.csv`,
`.env.example`) that no longer matches `main.py`'s actual flags
(`--tcgplayer-html`, no `.env.example` file present).

Common commands:
```bash
# Import
python main.py --tcgplayer-html order.html [--dry-run]   # TCGPlayer saved HTML → staging
python main.py --manual                                   # manual purchase entry
python main.py --ebay-import [--dry-run]                  # active eBay listings → staging
python main.py --ebay-verify [--account N]                # check eBay creds only, no writes

# Staging review/approve (required before anything reaches real inventory)
python main.py --review
python main.py --approve
python main.py --approve-order ORDER_NUM
python main.py --approve-all

# Reports
python main.py --stock

# eBay sales sync (scheduled via Windows Task Scheduler + the .bat files)
python main.py --ebay-pullorders [--since DATE] [--until DATE] [--paid-since DATE] [--quiet]
python main.py --ebay-syncfees [--since-days N]
python main.py --ebay-pullpicking [--account N]

# Local API the frontend calls to trigger a live picking refresh
uvicorn picking_api:app --host 0.0.0.0 --port 8765
```

`--account N` selects a numbered eBay account (`EBAY_ACCOUNT_{N}_*` in
`.env`); it defaults to 1. Multiple accounts are supported by simply adding
more numbered blocks.

`run_ebay_pull.bat`, `run_ebay_syncfees.bat`, `run_picking_api.bat` are what
Windows Task Scheduler actually runs on the always-on desktop — check these
before changing CLI flag names/behavior those jobs depend on.

**Known gap:** `main.py --ebay-reconcile` imports `importer.ebay_reconcile`,
which does not exist in this repo — that flag is currently broken.

## Architecture

### Layers

- `db/connection.py` — psycopg2 connection + all non-staging queries
  (`card_games`, `card_sets`, `card_master`, `card_variants`, `inventory`,
  `purchases`, `market_prices`, `import_corrections`). Every query goes
  through the `db_cursor()` context manager (commits on success, rolls back
  on exception).
- `db/staging.py` — queries for the `staging` table only.
- `importer/` — one module per data source/workflow, each wired to one or
  more `main.py` flags. See per-file docstrings; they're kept accurate and
  are the best source for how a given importer behaves.
- `utils/` — stateless helpers: `pokemon_api.py` (PokemonTCG API lookups +
  field parsing), `ebay_parser.py`, `pricing_engine.py`, `image_processor.py`,
  `r2_storage.py` (Cloudflare R2 uploads), `set_name_map.py` (TCGPlayer set
  label → PokemonTCG API set ID).
- `schema.sql` — meant to be the full DB schema, run once in Supabase's SQL
  editor. **Treat it as a starting point, not ground truth**: it does not
  include `card_variants`, `market_prices`, `import_corrections`,
  `sale_orders`, `sale_line_item_fees`, or `picking_queue`, all of which are
  used throughout the code — the live Supabase schema has evolved past this
  file. When you need real column definitions, read the query code in
  `db/connection.py` / `db/staging.py` rather than trusting `schema.sql`.

### Staging → review → approve pipeline

Every importer (TCGPlayer HTML, eBay listings) writes rows into the
`staging` table first — nothing touches `inventory` directly on import.
`importer/staging_workflow.py` implements `--review` (interactive fix-up:
conditions, quantities, ambiguous card matches, manual prices) and
`--approve` / `--approve-all` / `--approve-order` (push staged rows into
real `inventory` + `purchases` rows). Treat this staging boundary as
load-bearing when adding a new importer — write to staging, not inventory.

### Card variant model (seven axes)

Cards aren't just "holo/non-holo" — `card_variants` models a variant as up
to seven independent nullable axes: `foil_type`, `foil_pattern`, `texture`,
`material`, `size`, `stamp_type`, `source_type`. Identity is a generated
`variant_key` (null-safe concatenation of all seven), unique per
`(card_id, variant_key)`. `db.connection.get_or_create_variant()` is the
canonical way to resolve/insert a variant; `main.py`'s `_variant_label()`
and `cmd_fix_variant` show the full set of recognized axis values. When
adding new variant handling, extend this axis set rather than adding a
freeform "variant" string column (an old `card_master.variant` column was
explicitly removed in favor of this model — see the comment in
`find_card_by_name_set`).

### FIFO cost accounting

Each purchase creates its own `inventory` row with its own `cost_basis`
(no averaging across purchases). Sales are recorded via the Postgres
function `deduct_inventory_fifo()` (defined in `schema.sql`), which deducts
from the oldest batch first. `db.connection.get_stock_summary()` aggregates
across batches (`SUM(quantity - quantity_sold)`) for display/reporting only
— the underlying batches stay separate.

### Pricing

`utils/pricing_engine.py` computes list price via a 4-layer priority
fallback: card-level manual override → set-level config (multiplier +
floor) → price-tier table (market price → list price) → platform default.

### eBay integration: two separate auth models

- **Trading API (XML)** — `importer/ebay_auth.py`. Auth'n'Auth user tokens
  (`EBAY_ACCOUNT_{N}_TOKEN`, long-lived, no refresh flow). Used for listings
  import (`importer/ebay.py`) and variation renames
  (`importer/rename_variation.py`).
- **REST APIs (Finances + Fulfillment), OAuth** — `importer/ebay_finances.py`.
  Uses `EBAY_ACCOUNT_{N}_REFRESH_TOKEN` (~18-month lifespan) to mint
  short-lived access tokens, cached in-process per account per run. Used by
  `ebay_orders.py` (sales), `ebay_syncfees.py` (real fees/discounts/refunds),
  and `ebay_picking.py` (unshipped-order snapshot for the Picking tab).
  `rotate_ebay_token.py` is the standalone (not wired to `main.py`) helper
  for re-minting a refresh token via the OAuth consent flow when one is
  exposed or expiring.

Both auth models read from the same numbered `EBAY_ACCOUNT_{N}_*` block in
`.env`; account discovery for multi-account jobs (e.g. `--ebay-pullpicking`
with no `--account`) probes `EBAY_ACCOUNT_{N}_REFRESH_TOKEN` from N=1 until
a gap.

**Known bug:** `rename_variation.py` fails when eBay's
`VariationSpecificsSet` count doesn't match the active variation rows on
eBay's side — needs a manual fix in Seller Hub when it happens.

**Known deprecation:** eBay Picture Services (EPS), used for image uploads,
is being retired September 30, 2026 — image upload code will need to
migrate to the Media API before then.

`picking_queue` (written by `ebay_picking.py`) is a full snapshot, not an
incremental table — every run truncates and rewrites it in one transaction
across all accounts.

### TCGPlayer importers

Only `importer/tcgplayer_html.py` (parses saved TCGPlayer order HTML pages
via BeautifulSoup) is currently wired to a `main.py` flag (`--tcgplayer-html`).
`importer/tcgplayer.py` (CSV import) and `importer/tcgplayer_scraper.py`
(Selenium scraping) exist and are referenced in `README.md`/their own
docstrings but have no corresponding `main.py` flag — don't assume they're
reachable from the CLI without checking `main.py` first.

`Order2.html` / `Order3.html` (+ `_files/` asset folders) at the repo root
are saved TCGPlayer pages used as real fixtures for the HTML importer, not
part of any build output.

## Current priorities / TODO

- Market-price refresh (`importer/market_price_refresh.py`,
  `refresh_market_prices()`) is built and live — scoped per-set
  (`set_name`) or per-single-card (`card_id`), exposed via the web app's
  Jobs page (`POST /api/jobs/market-price-refresh` in `picking_api.py`).
  **Backlog, not started**: a "refresh market price" button on the
  Listing Pricing page (`card-board-mastermind-WebInvManagement/listing-pricing.js`)
  for a specific listing's roster. Fei's explicit requirement (2026-08-16):
  needs to update **only the cards picked within that listing**, not the
  whole set(s) the roster happens to span — a listing's roster can cross
  multiple sets (e.g. a themed listing spanning a whole era), so
  looping the existing per-set refresh isn't sufficient; that would
  touch cards outside the listing too. Needs `refresh_market_prices()`/
  `_cards_needing_refresh()` extended with an explicit `card_ids: list[str]`
  scoping path (today only `set_name` XOR `card_id`), then a UI reusing
  the roster table's existing bulk-selection pattern (`state.selected`,
  already used for bulk-group-assign/bulk-sync/bulk-delete) to pick
  which cards to refresh.
- ~~Per-copy photo library needs a backfill for already-live cards~~ —
  **done 2026-08-17**. `--ebay-backfill-card-photos` (`main.py`,
  `importer.card_photos.backfill_card_photos_from_ebay()`) fetches
  `Variations/Pictures/VariationSpecificPictureSet` via one `GetItem`
  call per unique live listing (grouped, not per-card — 65 unique
  listings backed all active roster rows), matches each entry's
  `VariationSpecificValue` against `platform_listings.external_id`, and
  seeds a `card_photos` row from the real, already-live picture URL.
  Re-runnable (`--dry-run`, `--force`, `--listing-id` supported). Ran
  against every `status IN ('active', 'out_of_stock')` roster row —
  9,467 of 9,472 filled (99.9%); the remaining 5 genuinely have no
  `VariationSpecificPictureSet` entry on eBay at all (no picture was
  ever attached to that exact live variation), not a backfill gap.
- ~~51 of 64 live listings have real eBay variations with NO
  `platform_listings` row at all~~ — **done 2026-08-17**, same session
  caught. `--ebay-adopt-untracked-variations --listing-id ITEM_ID`
  (`main.py`, `importer.ebay_pushprices.adopt_untracked_live_variations()`)
  fetches a listing's live `<Variation>` entries, matches each untracked
  one via the same `fetch_item_variations()`/`lookup_card_for_ebay()`
  pipeline `--ebay-import` uses, and inserts `platform_listings` +
  `listing_card_assignments` + `ebay_listing_map` rows reflecting what's
  already live (no eBay Revise call). Fei green-lit the remaining 50
  after reviewing the first test listing. **Result: every live listing
  now matches eBay exactly, including "Pokemon TCG Classic"
  (`335336413541`)** — initially deliberately skipped (unrecognized
  `CLV`/`CLC`/`CLB 001/034 <name>` naming scheme, 3 sub-packs each
  numbered independently), then actually fixed same session (see
  `utils/ebay_parser.py`'s `SET_PREFIX_OVERRIDES`, same generic
  prefix-override pattern as `PROMO_PATTERNS`) once it turned out to be
  a cheap, real fix rather than a one-off hack. Of its 27 untracked
  cards, 14 matched immediately; the other 13 (Squirtle, Magikarp,
  Articuno, Zapdos, Clefairy, Clefable, Mewtwo, Snorlax, Miltank LV32,
  Boss's Orders, Pokemon Nurse, Switch, Fire Energy) don't exist in the
  Pokemon TCG API at all (Fei confirmed) — created their `card_master`
  rows by hand from the known-correct eBay text (name/number/set only,
  no rarity/attributes — worth a manual pass later) so local-DB
  matching found them instantly on retry, no API needed.
  **Real bug fixed same session**: 10 of the first listing's 20 adopted
  cards got wrongly created as `non_holo` (Illustration Rare/Ultra Rare
  secret rares that only ever print holo, but have no "Holo"/"RH" text
  for the eBay-name parser to key off). Root cause was a `HOLO_RARITIES`
  correction in `write_to_staging()` running AFTER the variant was
  already resolved/created — fixed at the source by moving the check
  inside `lookup_card_for_ebay()` (`utils/pokemon_api.py`) itself,
  before variant creation.
  **Second real bug, found running the other 50 concurrently across
  3 overlapping batch jobs**: a duplicate-key crash
  (`idx_platform_listings_unique`) aborted a listing's entire remaining
  work when the same variant was already tracked under a different
  eBay text — mostly a race between the overlapping runs themselves
  (one run's "already tracked" snapshot went stale mid-loop as another
  run committed concurrently), confirmed by re-running the crashed
  listings serially with no concurrency and having 6 of 7 resolve
  clean with zero errors. Fixed by checking for an existing
  `(platform, account, listing_id, variant_id)` row before each INSERT
  instead of relying on the constraint violation, so one bad/racy row
  no longer kills the rest of that listing's adoption.
  **Third finding, a genuinely stale row** (`336458632284`,
  "Mega Evolution 2.5 Ascended Heroes"): `141/217 Hoopa` (non_holo) was
  a real pre-existing roster row whose eBay text had drifted — the live
  listing now calls it `141/217 Hoopa Holo` (a real, different, holo
  variant that adoption correctly added as a new row). The old
  non_holo row was never actually removed from eBay through this app
  (Seller Hub edit, same "attached outside this app" pattern as
  everything else this session) — reconciled by marking it `delisted`
  (`sync_enabled = false`) and its roster row back to `queued`, the
  same pattern `remove_single_card_live()` already uses for a real
  removal.
  **Fourth finding — the "2 listings with the opposite mismatch"
  flagged earlier turned out to be a false alarm**, not a real problem:
  the original survey counted ALL `platform_listings` rows for a
  listing_id with no status filter, so a normal, legitimately-kept
  `status='delisted'` row (audit history, unrelated to any of this
  session's work) inflated the count above the live variation count.
  Re-checked with `status != 'delisted'` excluded and both listings
  (`336204674240`, `336691613250`) balance exactly. Lesson: never
  compare a raw `platform_listings` row count to eBay's live variation
  count without excluding `delisted` — it's expected to accumulate
  over time and isn't itself a sign of a problem.
  **Full DB-wide cleanup also done same session**: Fei asked for the
  complete always-holo rarity list (`Ace Spec Rare`, `Double Rare`,
  `Hyper Rare`, `Illustration Rare`, `Mega Hyper Rare`,
  `MEGA_ATTACK_RARE`, `Shiny Rare`, `Shiny Ultra Rare`, `Ultra Rare` —
  added the 4 missing ones to `HOLO_RARITIES`, which also collapsed 3
  duplicate copies of that set across `utils/pokemon_api.py`/
  `importer/ebay.py` into one shared constant). A full-DB scan found
  **501 pre-existing misclassified `card_variants` rows** (290
  Illustration Rare, 127 Ultra Rare, 34 ACE SPEC Rare, 29 Special
  Illustration Rare, 21 Hyper Rare) predating this session entirely —
  495 were pure orphans (zero references anywhere, safely deleted or
  flipped in place), 6 were tied to a real listing/inventory/photo/
  **sales** row and got carefully repointed instead. **Caught a real
  gap in the repoint script mid-run**: it didn't check the `sales`
  table for references, so `DELETE FROM card_variants` hit a live FK
  violation on a card with real sale history (`Brave Bangle` #104,
  Pitch Black) — fixed the check and the specific row, then re-ran
  clean. Fixed listing-by-listing at Fei's request (lowest count
  first), all 501 now correct.
- **`platform_listings.quantity_listed` goes stale on every normal
  price/qty sync** (found 2026-08-17, code fixed same session — see
  `importer/ebay_pushprices.py`, both `UPDATE platform_listings` sites
  in `push_prices()` now set `quantity_listed` alongside `pushed_qty`).
  This column is the ONLY one `resolve_listing_prices()`'s shared-
  inventory subtraction reads, so a stale value makes every OTHER
  listing sharing that card think more is available than really is.
  **9,197 of ~9,467 active rows account-wide were stale** when found —
  **only backfilled for the two Chaos Rising listings and the two
  Pitch Black listings so far** (using each row's real live eBay
  quantity via `fetch_item_variations()`, NOT `pushed_qty` — that
  column is NULL on many older rows since it was added later than
  `quantity_listed`; a first attempt at backfilling via
  `quantity_listed = pushed_qty` wiped 75 rows to NULL before being
  caught and fixed the same way). **Backlog, not started**: the
  remaining ~9,000+ stale rows across the rest of the account need the
  same live-eBay-read backfill, listing by listing or in bulk.
- "Pending shipment" feature: pull paid-but-unshipped eBay orders for a
  pick/pack workflow.
- Next architecture decision: auto-refreshing inventory as eBay orders
  sell — options being weighed are cron/launchd polling, eBay webhooks, or
  Supabase realtime subscriptions on the frontend Inventory tab.

## Secrets

`.env` (gitignored) holds DB credentials, per-account eBay tokens, the
PokemonTCG API key, R2 storage credentials, and `PICKING_API_TOKEN`. There
is no `.env.example` in the repo despite `README.md` referencing one — when
adding a new required env var, add it to the relevant module's docstring
(the existing convention, e.g. see the header comments in `ebay_auth.py`,
`ebay_finances.py`, `picking_api.py`) rather than creating that file.
