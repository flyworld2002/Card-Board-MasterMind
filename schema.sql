-- ============================================================
-- Card Inventory Database Schema
-- PostgreSQL / Supabase
-- ============================================================

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ------------------------------------------------------------
-- card_games
-- Top-level game registry. Add a row when expanding beyond Pokemon.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_games (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    publisher   TEXT,
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO card_games (name, publisher, notes)
VALUES ('Pokemon', 'Nintendo / The Pokemon Company', 'Initial game')
ON CONFLICT (name) DO NOTHING;

-- ------------------------------------------------------------
-- card_sets
-- One row per set per language.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_sets (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    game_id      UUID NOT NULL REFERENCES card_games(id),
    name         TEXT NOT NULL,
    series       TEXT,
    set_code     TEXT NOT NULL,
    release_year INT,
    language     TEXT NOT NULL DEFAULT 'English',
    total_cards  INT,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (set_code, language)
);

-- ------------------------------------------------------------
-- card_master
-- One row per unique printable card.
-- Variant/finish/promo flags live here — game-agnostic.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_master (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    set_id           UUID NOT NULL REFERENCES card_sets(id),
    name             TEXT NOT NULL,
    card_number      TEXT NOT NULL,
    rarity           TEXT,
    variant          TEXT,                        -- e.g. 'Reverse Holo', 'Full Art', 'Rainbow Rare'
    finish           TEXT,                        -- e.g. 'Holo', 'Non-Holo', 'Etched'
    is_promo         BOOLEAN DEFAULT FALSE,
    is_first_edition BOOLEAN DEFAULT FALSE,
    image_url        TEXT,                        -- stock image from PokemonTCG API (auto-populated)
    image_url_own    TEXT,                        -- your own photo, hosted on Cloudflare R2
    external_id      TEXT UNIQUE,                 -- PokemonTCG API card ID e.g. 'base1-4'
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (set_id, card_number, variant)
);

-- ------------------------------------------------------------
-- card_attributes
-- Pokemon-specific fields. Add game-specific tables later
-- (e.g. mtg_attributes) without touching card_master.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_attributes (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id       UUID NOT NULL UNIQUE REFERENCES card_master(id) ON DELETE CASCADE,
    card_type     TEXT,                           -- e.g. 'Pokemon', 'Trainer', 'Energy'
    stage         TEXT,                           -- e.g. 'Basic', 'Stage 1', 'Stage 2'
    hp            INT,
    energy_type   TEXT,                           -- e.g. 'Fire', 'Water', 'Psychic'
    artist        TEXT,
    weakness      TEXT,
    resistance    TEXT,
    retreat_cost  INT
);

-- ------------------------------------------------------------
-- purchases
-- One row per buying event (TCGPlayer order, eBay lot, card show, etc.)
-- Supports both individual buys and bulk lots.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS purchases (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source        TEXT NOT NULL,                  -- 'tcgplayer', 'ebay', 'local_shop', 'card_show', 'trade'
    purchase_type TEXT NOT NULL DEFAULT 'single', -- 'single', 'lot', 'collection'
    reference_id  TEXT,                           -- TCGPlayer order number, eBay order ID, etc.
    total_cost    NUMERIC(10,2) NOT NULL,
    card_count    INT,
    notes         TEXT,
    purchased_at  TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- inventory
-- One row per card + condition + purchase batch.
-- FIFO: multiple rows for the same card at different costs.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id         UUID NOT NULL REFERENCES card_master(id),
    purchase_id     UUID REFERENCES purchases(id),
    condition       TEXT NOT NULL,                -- 'Near Mint', 'Lightly Played', 'Moderately Played', 'Heavily Played', 'Damaged'
    is_graded       BOOLEAN DEFAULT FALSE,
    quantity        INT NOT NULL DEFAULT 1 CHECK (quantity >= 0),
    quantity_sold   INT NOT NULL DEFAULT 0 CHECK (quantity_sold >= 0),
    cost_basis      NUMERIC(10,2) NOT NULL,       -- price paid per card (total_cost / qty)
    asking_price    NUMERIC(10,2),
    market_price    NUMERIC(10,2),                -- refreshed periodically from TCGPlayer/eBay
    market_price_updated_at TIMESTAMPTZ,
    notes           TEXT,
    acquired_at     TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT qty_check CHECK (quantity_sold <= quantity)
);

-- Computed quantity available (quantity - quantity_sold)
-- Use this everywhere instead of raw quantity
CREATE OR REPLACE VIEW inventory_available AS
SELECT
    i.*,
    cm.name         AS card_name,
    cm.card_number,
    cm.rarity,
    cm.variant,
    cm.finish,
    cm.is_promo,
    cm.is_first_edition,
    cs.name         AS set_name,
    cs.set_code,
    cs.release_year,
    cg.name         AS game_name,
    (i.quantity - i.quantity_sold) AS quantity_available
FROM inventory i
JOIN card_master cm ON i.card_id = cm.id
JOIN card_sets cs   ON cm.set_id = cs.id
JOIN card_games cg  ON cs.game_id = cg.id
WHERE (i.quantity - i.quantity_sold) > 0;

-- ------------------------------------------------------------
-- grading_info
-- Optional. Attached to an inventory row when is_graded = TRUE.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grading_info (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    inventory_id     UUID NOT NULL UNIQUE REFERENCES inventory(id) ON DELETE CASCADE,
    grading_company  TEXT NOT NULL,               -- 'PSA', 'BGS', 'CGC', 'SGC'
    grade            NUMERIC(4,1) NOT NULL,        -- e.g. 9.5, 10
    cert_number      TEXT UNIQUE,
    label_type       TEXT,                         -- e.g. 'Standard', 'Pristine', 'Black Label'
    graded_at        TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- platform_listings
-- One row per active listing per platform per inventory batch.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platform_listings (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    inventory_id UUID NOT NULL REFERENCES inventory(id),
    platform     TEXT NOT NULL,                   -- 'ebay', 'shopify', 'tcgplayer', 'amazon', 'whatnot'
    external_id  TEXT,                            -- platform's listing/product ID
    list_price   NUMERIC(10,2) NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',  -- 'active', 'ended', 'sold', 'draft'
    listed_at    TIMESTAMPTZ DEFAULT NOW(),
    synced_at    TIMESTAMPTZ,
    UNIQUE (platform, external_id)
);

-- ------------------------------------------------------------
-- customers  [Phase 2]
-- One row per unique buyer across all platforms.
-- Linked to sale_events for purchase history and geo reporting.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name        TEXT,
    email            TEXT,
    phone            TEXT,
    address_line1    TEXT,
    address_line2    TEXT,
    city             TEXT,
    state            TEXT,
    zip              TEXT,
    country          TEXT NOT NULL DEFAULT 'US',
    source_platform  TEXT,
    external_id      TEXT,                        -- platform buyer ID / username
    first_purchase_at TIMESTAMPTZ,
    notes            TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_platform, external_id)
);

CREATE INDEX IF NOT EXISTS idx_customers_state   ON customers(state);
CREATE INDEX IF NOT EXISTS idx_customers_country ON customers(country);
CREATE INDEX IF NOT EXISTS idx_customers_source  ON customers(source_platform);

-- ------------------------------------------------------------
-- sale_events
-- Immutable audit log. One row per sale.
-- net_profit computed and stored at sale time.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sale_events (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    inventory_id   UUID NOT NULL REFERENCES inventory(id),
    listing_id     UUID REFERENCES platform_listings(id),
    customer_id    UUID REFERENCES customers(id),  -- optional; linked when buyer info available
    platform       TEXT NOT NULL,
    quantity_sold  INT NOT NULL DEFAULT 1,
    sale_price     NUMERIC(10,2) NOT NULL,
    platform_fee   NUMERIC(10,2) DEFAULT 0,
    shipping_cost  NUMERIC(10,2) DEFAULT 0,
    cost_basis     NUMERIC(10,2) NOT NULL,        -- snapshot of cost_basis at time of sale
    net_profit     NUMERIC(10,2) GENERATED ALWAYS AS
                   (sale_price - platform_fee - shipping_cost - cost_basis) STORED,
    sold_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ------------------------------------------------------------
-- FIFO deduction function
-- Called on every sale. Deducts from oldest batch first.
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION deduct_inventory_fifo(
    p_card_id     UUID,
    p_condition   TEXT,
    p_qty         INT,
    p_platform    TEXT,
    p_sale_price  NUMERIC,
    p_platform_fee NUMERIC DEFAULT 0,
    p_shipping    NUMERIC DEFAULT 0
) RETURNS VOID AS $$
DECLARE
    v_row        RECORD;
    v_remaining  INT := p_qty;
    v_deduct     INT;
BEGIN
    -- Walk inventory batches oldest-first for this card + condition
    FOR v_row IN
        SELECT id, quantity, quantity_sold, cost_basis
        FROM inventory
        WHERE card_id   = p_card_id
          AND condition = p_condition
          AND is_graded = FALSE
          AND (quantity - quantity_sold) > 0
        ORDER BY acquired_at ASC
    LOOP
        EXIT WHEN v_remaining = 0;

        v_deduct := LEAST(v_remaining, v_row.quantity - v_row.quantity_sold);

        -- Deduct from this batch
        UPDATE inventory
        SET quantity_sold = quantity_sold + v_deduct,
            updated_at    = NOW()
        WHERE id = v_row.id;

        -- Record the sale event
        INSERT INTO sale_events
            (inventory_id, platform, quantity_sold, sale_price, platform_fee, shipping_cost, cost_basis, sold_at)
        VALUES
            (v_row.id, p_platform, v_deduct, p_sale_price, p_platform_fee, p_shipping, v_row.cost_basis, NOW());

        v_remaining := v_remaining - v_deduct;
    END LOOP;

    IF v_remaining > 0 THEN
        RAISE EXCEPTION 'Insufficient inventory: % units still unallocated for card %', v_remaining, p_card_id;
    END IF;
END;
$$ LANGUAGE plpgsql;

-- ------------------------------------------------------------
-- Useful indexes
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_inventory_card_id    ON inventory(card_id);
CREATE INDEX IF NOT EXISTS idx_inventory_acquired   ON inventory(acquired_at);
CREATE INDEX IF NOT EXISTS idx_inventory_condition  ON inventory(condition);
CREATE INDEX IF NOT EXISTS idx_card_master_set      ON card_master(set_id);
CREATE INDEX IF NOT EXISTS idx_card_master_name     ON card_master(name);
CREATE INDEX IF NOT EXISTS idx_sale_events_sold_at  ON sale_events(sold_at);
CREATE INDEX IF NOT EXISTS idx_platform_listings_platform ON platform_listings(platform, status);

-- ------------------------------------------------------------
-- Geography reporting view  [Phase 2]
-- "Where do I do most of my business?"
-- Usage: SELECT * FROM sales_by_state ORDER BY total_sales DESC;
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW sales_by_state AS
SELECT
    COALESCE(c.state, 'Unknown')    AS state,
    COALESCE(c.country, 'Unknown')  AS country,
    COUNT(DISTINCT c.id)            AS unique_buyers,
    COUNT(se.id)                    AS total_orders,
    SUM(se.quantity_sold)           AS total_cards_sold,
    ROUND(SUM(se.sale_price), 2)    AS total_revenue,
    ROUND(SUM(se.net_profit), 2)    AS total_profit,
    ROUND(AVG(se.sale_price), 2)    AS avg_order_value
FROM sale_events se
LEFT JOIN customers c ON se.customer_id = c.id
GROUP BY c.state, c.country;

-- ------------------------------------------------------------
-- Customer lifetime value view  [Phase 2]
-- Usage: SELECT * FROM customer_lifetime_value ORDER BY lifetime_revenue DESC LIMIT 20;
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW customer_lifetime_value AS
SELECT
    c.id,
    c.full_name,
    c.email,
    c.city,
    c.state,
    c.source_platform,
    c.first_purchase_at,
    COUNT(se.id)                    AS total_orders,
    SUM(se.quantity_sold)           AS total_cards_bought,
    ROUND(SUM(se.sale_price), 2)    AS lifetime_revenue,
    ROUND(AVG(se.sale_price), 2)    AS avg_order_value,
    MAX(se.sold_at)                 AS last_purchase_at
FROM customers c
LEFT JOIN sale_events se ON se.customer_id = c.id
GROUP BY c.id, c.full_name, c.email, c.city, c.state,
         c.source_platform, c.first_purchase_at;

-- ============================================================
-- PHASE 2: PRICING ENGINE + STAGING
-- ============================================================

-- ------------------------------------------------------------
-- price_tiers
-- The core tier system: market price range → your list price.
-- One row per tier per platform. Editable anytime.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_tiers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform        TEXT NOT NULL DEFAULT 'ebay',
    card_type       TEXT NOT NULL DEFAULT 'common', -- 'common', 'reverse_holo', 'holo', 'ultra_rare'
    market_price_max NUMERIC(10,2) NOT NULL,         -- upper bound of this tier
    list_price      NUMERIC(10,2) NOT NULL,           -- your price at this tier
    sort_order      INT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (platform, card_type, market_price_max)
);

-- Your default eBay tiers
INSERT INTO price_tiers (platform, card_type, market_price_max, list_price, sort_order) VALUES
  -- Common / non-holo
  ('ebay', 'common', 0.10,  0.99, 1),
  ('ebay', 'common', 0.20,  1.37, 2),
  ('ebay', 'common', 0.25,  1.49, 3),
  ('ebay', 'common', 0.35,  1.99, 4),
  ('ebay', 'common', 0.50,  2.49, 5),
  ('ebay', 'common', 1.00,  4.99, 6),
  ('ebay', 'common', 2.00,  5.99, 7),
  -- Reverse holo (slight discount vs common at $1 tier)
  ('ebay', 'reverse_holo', 0.10,  0.99, 1),
  ('ebay', 'reverse_holo', 0.20,  1.37, 2),
  ('ebay', 'reverse_holo', 0.25,  1.49, 3),
  ('ebay', 'reverse_holo', 0.35,  1.99, 4),
  ('ebay', 'reverse_holo', 0.50,  2.49, 5),
  ('ebay', 'reverse_holo', 1.00,  3.99, 6),
  ('ebay', 'reverse_holo', 2.00,  5.99, 7),
  -- Holo rare
  ('ebay', 'holo', 0.10,  1.49, 1),
  ('ebay', 'holo', 0.20,  1.49, 2),
  ('ebay', 'holo', 0.25,  1.49, 3),
  ('ebay', 'holo', 0.35,  1.99, 4),
  ('ebay', 'holo', 0.50,  2.49, 5),
  ('ebay', 'holo', 1.00,  4.99, 6),
  ('ebay', 'holo', 2.00,  5.99, 7)
ON CONFLICT (platform, card_type, market_price_max) DO NOTHING;

-- ------------------------------------------------------------
-- set_pricing_config
-- Per-set pricing rules. One row per set per platform.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS set_pricing_config (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    set_id              UUID NOT NULL REFERENCES card_sets(id),
    platform            TEXT NOT NULL DEFAULT 'ebay',
    -- Multiplier applied on top of tier price
    price_multiplier    NUMERIC(5,2) NOT NULL DEFAULT 1.00,
    -- Floor prices (override tier minimums)
    common_floor        NUMERIC(10,2),               -- e.g. 1.49 for popular sets
    reverse_holo_floor  NUMERIC(10,2),
    holo_floor          NUMERIC(10,2),
    -- Higher rarity pricing rule
    ultra_rare_rule     TEXT NOT NULL DEFAULT 'tier', -- 'tier', 'manual', 'multiplier'
    ultra_rare_multiplier NUMERIC(5,2) DEFAULT 2.00,  -- used when rule = 'multiplier'
    ultra_rare_plus     NUMERIC(10,2) DEFAULT 1.00,   -- added after multiplier
    -- eBay listing structure
    common_max_card_num INT,                          -- e.g. 165 for 151
    set_total_cards     INT,                          -- e.g. 207 for 151
    -- Set classification
    era_tag             TEXT,                         -- 'popular', 'vintage', 'standard', 'rotation'
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (set_id, platform)
);

-- ------------------------------------------------------------
-- card_pricing_overrides
-- Manual price override per card per platform.
-- Highest priority — overrides everything else.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_pricing_overrides (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    card_id      UUID NOT NULL REFERENCES card_master(id),
    platform     TEXT NOT NULL DEFAULT 'ebay',
    list_price   NUMERIC(10,2) NOT NULL,
    notes        TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (card_id, platform)
);

-- ------------------------------------------------------------
-- listing_templates
-- Defines eBay listing structure (commons vs reverse holo/ultra rare).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listing_templates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform        TEXT NOT NULL DEFAULT 'ebay',
    name            TEXT NOT NULL,                    -- 'commons', 'reverse_holo_ultra'
    description     TEXT,
    -- Card type filters
    included_types  TEXT[],                           -- e.g. ARRAY['common','holo','double_rare']
    excluded_types  TEXT[],                           -- e.g. ARRAY['reverse_holo']
    -- Card number range (null = no limit)
    card_num_min    INT,
    card_num_max    INT,                              -- set from set_pricing_config.common_max_card_num
    -- Shipping
    shipping_base   NUMERIC(10,2) DEFAULT 0.00,       -- 0 = free
    shipping_per_card NUMERIC(10,2) DEFAULT 0.00,     -- 0.10 for reverse holo listing
    -- eBay limits
    max_quantity    INT DEFAULT 250,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (platform, name)
);

INSERT INTO listing_templates
    (platform, name, description, included_types, excluded_types,
     shipping_base, shipping_per_card, max_quantity)
VALUES
  ('ebay', 'commons',
   'Common, trainer, holo, double rare — free shipping',
   ARRAY['common','uncommon','trainer','holo','double_rare'],
   ARRAY['reverse_holo'],
   0.00, 0.00, 250),
  ('ebay', 'reverse_holo_ultra',
   'Reverse holo, ultra rare, SIR, hyper rare — $0.79 + $0.10/card',
   ARRAY['reverse_holo','holo','double_rare','illustration_rare','special_illustration_rare','ultra_rare','hyper_rare'],
   ARRAY[]::TEXT[],
   0.79, 0.10, 250)
ON CONFLICT (platform, name) DO NOTHING;

-- ------------------------------------------------------------
-- staging
-- Holding area for all imported orders before they hit inventory.
-- Every import lands here first — you review, fix, then approve.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    import_batch    TEXT NOT NULL,                    -- timestamp-based batch ID
    -- Order info
    order_number    TEXT,
    order_date      TIMESTAMPTZ,
    source          TEXT NOT NULL DEFAULT 'tcgplayer',
    -- Card info (editable before approval)
    card_name       TEXT NOT NULL,
    set_name        TEXT,
    card_number     TEXT,
    condition       TEXT NOT NULL DEFAULT 'Near Mint',
    quantity        INT NOT NULL DEFAULT 1,
    price           NUMERIC(10,2) NOT NULL DEFAULT 0,  -- cost basis per card
    -- Resolution
    card_id         UUID REFERENCES card_master(id),   -- null if not yet matched
    match_status    TEXT NOT NULL DEFAULT 'pending',   -- 'matched', 'ambiguous', 'not_found'
    match_options   JSONB,                             -- API results when ambiguous
    -- Calculated pricing (populated on approval)
    calculated_price NUMERIC(10,2),                   -- what the pricing engine suggests
    override_price   NUMERIC(10,2),                   -- your manual override if any
    -- Workflow status
    status          TEXT NOT NULL DEFAULT 'pending',   -- 'pending','approved','skipped'
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_staging_batch   ON staging(import_batch);
CREATE INDEX IF NOT EXISTS idx_staging_status  ON staging(status);
CREATE INDEX IF NOT EXISTS idx_staging_order   ON staging(order_number);
