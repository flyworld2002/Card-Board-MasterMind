-- Migration 001: eBay listing sync — new tables + column adds
-- Generated from docs/plans/ebay-listing-sync.md (revised DDL section,
-- post live-DB Step 0 findings). All additive: new tables, nullable/
-- defaulted columns, one trigger. No drops, no data loss risk.
-- Run once against the live Supabase DB (no migrations/ tooling in this repo).

BEGIN;

-- ------------------------------------------------------------
-- listing_templates
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listing_templates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform text NOT NULL DEFAULT 'ebay',
  account text,
  name text NOT NULL,
  base_price numeric,
  default_quantity_limit integer,
  low_stock_threshold integer DEFAULT 8,
  low_stock_bump integer DEFAULT 1,
  listing_kind text NOT NULL DEFAULT 'variation',
  priority_rule text DEFAULT 'card_number',
  display_sort text DEFAULT 'card_number',
  name_format text DEFAULT '{number}/{set_total} {name} {suffix}',
  card_type_filter text[],
  notes text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

-- ------------------------------------------------------------
-- card_type_mapping (7-axis -> pricing type)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_type_mapping (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform text NOT NULL DEFAULT 'ebay',
  account text,
  foil_type text,
  foil_pattern text,
  texture text,
  material text,
  size text,
  stamp_type text,
  source_type text,
  tier_card_type text NOT NULL,
  priority integer NOT NULL DEFAULT 0,
  created_at timestamptz DEFAULT now()
);

-- ------------------------------------------------------------
-- platform_sync_status (platform + account kill switch)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platform_sync_status (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform text NOT NULL DEFAULT 'ebay',
  account text,
  sync_enabled boolean NOT NULL DEFAULT true,
  disabled_at timestamptz,
  notes text,
  UNIQUE (platform, account)
);

-- ------------------------------------------------------------
-- listing_card_assignments (intent layer for the 250-cap)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listing_card_assignments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform_listing_id uuid NOT NULL REFERENCES platform_listings(id),
  variant_id uuid NOT NULL REFERENCES card_variants(id),
  priority_rank integer NOT NULL,
  status text NOT NULL DEFAULT 'queued',
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  UNIQUE (platform_listing_id, variant_id)
);

-- ------------------------------------------------------------
-- platform_listings column additions
-- (quantity_limit and synced_at already exist — not touched here)
-- ------------------------------------------------------------
ALTER TABLE platform_listings ADD COLUMN IF NOT EXISTS template_id uuid REFERENCES listing_templates(id);
ALTER TABLE platform_listings ADD COLUMN IF NOT EXISTS base_price numeric;
ALTER TABLE platform_listings ADD COLUMN IF NOT EXISTS sync_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE platform_listings ADD COLUMN IF NOT EXISTS price_synced_at timestamptz;
ALTER TABLE platform_listings ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

-- Trigger: bump updated_at whenever list_price changes (drives the
-- "pending changes" signal: updated_at > price_synced_at).
CREATE OR REPLACE FUNCTION bump_platform_listings_updated_at()
RETURNS trigger AS $$
BEGIN
  IF NEW.list_price IS DISTINCT FROM OLD.list_price THEN
    NEW.updated_at := now();
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_platform_listings_updated_at ON platform_listings;
CREATE TRIGGER trg_platform_listings_updated_at
  BEFORE UPDATE ON platform_listings
  FOR EACH ROW
  EXECUTE FUNCTION bump_platform_listings_updated_at();

-- ------------------------------------------------------------
-- set_pricing_config / card_pricing_overrides column additions
-- ------------------------------------------------------------
ALTER TABLE set_pricing_config ADD COLUMN IF NOT EXISTS tier_bump integer NOT NULL DEFAULT 0;
ALTER TABLE card_pricing_overrides ADD COLUMN IF NOT EXISTS quantity_limit integer;

-- card_sets.official_set_total intentionally NOT added — card_sets.total_cards
-- already serves this purpose (confirmed live in Configuration UI).

COMMIT;
