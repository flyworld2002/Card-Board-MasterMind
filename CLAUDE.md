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
- **Per-copy photo library needs a backfill for already-live cards**
  (`card_photos`/`card_photo_details`, `importer/card_photos.py`, added
  2026-08-17). Confirmed live (2026-08-17): 9,470 `listing_card_assignments`
  rows are `status='active'` (already live on eBay, almost certainly with
  real pictures attached), but only 1 row ever went through this app's
  picture-staging flow and `card_photos` has 0 rows — meaning virtually
  every existing live picture was attached directly through eBay's own
  tools, outside anything this codebase ever tracked. The new "Manage
  photos" UI (`listing-pricing.js`) only offers photos staged *through
  it* going forward, so it shows "No existing photos" for cards that
  visibly already have one live on eBay. **Not started**: a backfill
  that calls `GetItem` per already-active card, pulls the real photo URL
  from `Variations/Pictures/VariationSpecificPictureSet` for that
  specific variation (same XML shape/read pattern as the
  `--ebay-backfill-nav-images` fix below, but scoped per-card instead of
  per-template, and against ~9,470 rows instead of ~54 templates), and
  seeds a `card_photos` row from it so those pictures become reusable
  the next time that same card gets added to a different listing.
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
