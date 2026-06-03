# Card Inventory Import Tool

Python tool to import Pokemon card purchases into a PostgreSQL (Supabase) database.
Supports TCGPlayer CSV exports, manual entry, and FIFO cost tracking.

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Create your Supabase database
1. Go to https://supabase.com and create a free account
2. Create a new project
3. Go to **SQL Editor** and paste the contents of `schema.sql` — run it
4. Go to **Settings → Database** and copy your connection credentials

### 3. Configure credentials
```bash
cp .env.example .env
# Edit .env with your Supabase credentials
```

---

## Usage

### Import a TCGPlayer order CSV
1. Log into TCGPlayer → **My Account → Order History**
2. Click **Export to CSV** (top right of order history)
3. Run:
```bash
python main.py --tcgplayer orders.csv
```

Dry run first to check for issues without writing to the database:
```bash
python main.py --tcgplayer orders.csv --dry-run
```

### Manual entry (eBay, card shows, trades, etc.)
```bash
python main.py --manual
```
Follow the prompts. You'll enter the purchase source, date, then each card one by one.
Ambiguous card matches are shown as a numbered list — pick the right one.

### View current stock
```bash
python main.py --stock
```

---

## How FIFO works

Each purchase creates its own `inventory` row with its own `cost_basis`.
When a sale is recorded, the database function `deduct_inventory_fifo()` automatically
deducts from the oldest batch first. To record a sale directly in SQL:

```sql
SELECT deduct_inventory_fifo(
    p_card_id      := 'your-card-uuid',
    p_condition    := 'Near Mint',
    p_qty          := 1,
    p_platform     := 'ebay',
    p_sale_price   := 45.00,
    p_platform_fee := 4.05,
    p_shipping     := 4.50
);
```

---

## Project structure

```
card_inventory/
├── main.py              # CLI entry point
├── schema.sql           # Full database schema — run once in Supabase
├── requirements.txt
├── .env.example         # Copy to .env and fill in credentials
├── db/
│   └── connection.py    # Database layer (queries, inserts)
├── importer/
│   ├── tcgplayer.py     # TCGPlayer CSV parser + importer
│   └── manual.py        # Interactive manual entry
└── utils/
    └── pokemon_api.py   # PokemonTCG API lookup + field parsing
```

---

## Expanding to other card games

1. Add a row to `card_games` for the new game
2. Add sets to `card_sets` under that game
3. Create a game-specific attributes table (e.g. `mtg_attributes`) mirroring `card_attributes`
4. Add an importer under `importer/` for that game's data source

The `inventory`, `purchases`, `platform_listings`, and `sale_events` tables
are fully game-agnostic — no changes needed there.
