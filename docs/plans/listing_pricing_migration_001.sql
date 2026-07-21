-- Migration 001: Listing Pricing System (profiles + rules + pins)
-- Generated from docs/plans/listing-pricing-system.md (corrected DDL —
-- uuid PKs, platform_listings not platform_listing_lines, listing keyed
-- by shared eBay item_id not a single row's PK). All additive.

BEGIN;

CREATE TABLE IF NOT EXISTS pricing_profiles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,          -- e.g. 'double_rare_rh_ur'
  notes text,
  default_low_stock_qty integer,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pricing_profile_tiers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_id uuid NOT NULL REFERENCES pricing_profiles(id) ON DELETE CASCADE,
  min_market numeric(10,2) NOT NULL,   -- inclusive
  max_market numeric(10,2),            -- exclusive; NULL = open-ended top tier
  list_price numeric(10,2) NOT NULL
);

-- Enforce non-overlapping [min_market, max_market) tiers per profile.
-- NULL max_market = open-ended top tier (treated as +infinity for overlap
-- purposes).
CREATE OR REPLACE FUNCTION check_pricing_tier_no_overlap()
RETURNS trigger AS $$
DECLARE
  v_conflict record;
  v_open_ended CONSTANT numeric := 999999999.99;  -- numeric has no 'infinity' literal (float8-only)
BEGIN
  SELECT id, min_market, max_market INTO v_conflict
  FROM pricing_profile_tiers
  WHERE profile_id = NEW.profile_id
    AND id IS DISTINCT FROM NEW.id
    AND NEW.min_market < COALESCE(max_market, v_open_ended)
    AND COALESCE(NEW.max_market, v_open_ended) > min_market
  LIMIT 1;

  IF FOUND THEN
    RAISE EXCEPTION 'Tier [%, %) overlaps existing tier % [%, %) on profile %',
      NEW.min_market, NEW.max_market, v_conflict.id, v_conflict.min_market,
      v_conflict.max_market, NEW.profile_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS listing_pricing_rules (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform text NOT NULL DEFAULT 'ebay',
  listing_id text NOT NULL,            -- the shared eBay ItemID (platform_listings.listing_id) —
                                        -- NOT a FK to platform_listings.id, since one eBay
                                        -- listing spans many platform_listings rows (confirmed
                                        -- up to 244 for one listing_id).
  profile_id uuid NOT NULL REFERENCES pricing_profiles(id),
  match_rarity text,                   -- from card_master.rarity; NULL = wildcard
  match_foil_type text REFERENCES foil_types(code),
  match_set_id uuid REFERENCES card_sets(id),
  match_card_id uuid REFERENCES card_master(id),
  priority integer NOT NULL DEFAULT 100,
  low_stock_qty integer,               -- overrides profile default
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_listing_pricing_rules_listing
  ON listing_pricing_rules(platform, listing_id);

DROP TRIGGER IF EXISTS trg_pricing_tier_no_overlap ON pricing_profile_tiers;
CREATE TRIGGER trg_pricing_tier_no_overlap
  BEFORE INSERT OR UPDATE ON pricing_profile_tiers
  FOR EACH ROW
  EXECUTE FUNCTION check_pricing_tier_no_overlap();

ALTER TABLE platform_listings ADD COLUMN IF NOT EXISTS manual_price numeric(10,2);
  -- NULL = follow rules; set = pinned, sync never overwrites.
ALTER TABLE platform_listings ADD COLUMN IF NOT EXISTS low_stock_qty integer;
  -- per-row override of the rule/profile default.
ALTER TABLE platform_listings ADD COLUMN IF NOT EXISTS pushed_price numeric(10,2);
ALTER TABLE platform_listings ADD COLUMN IF NOT EXISTS pushed_qty integer;
ALTER TABLE platform_listings ADD COLUMN IF NOT EXISTS pushed_at timestamptz;
  -- pushed_* is an AUDIT SNAPSHOT of what was last sent to the platform —
  -- never a price source. resolved_price is always computed fresh by
  -- resolve_listing_prices(), never stored.

-- ------------------------------------------------------------
-- Seed: the two example profiles from the spec. Only the profiles + tiers
-- (no ambiguity there) — NOT the listing_pricing_rules attaching them to
-- real listings, since that needs Fei to confirm which real listing_id
-- values are "the commons listing" / "the reverse_holo_ultra listing"
-- (Open Question 4 in listing-pricing-system.md).
-- ------------------------------------------------------------
INSERT INTO pricing_profiles (name, notes) VALUES
  ('double_rare_common', 'Free-shipping bulk/commons listing pricing for Double Rare cards'),
  ('double_rare_rh_ur', 'Charged-shipping RH-to-ultra-rare listing pricing for Double Rare cards')
ON CONFLICT (name) DO NOTHING;

INSERT INTO pricing_profile_tiers (profile_id, min_market, max_market, list_price)
SELECT id, 0.00, 1.00, 4.99 FROM pricing_profiles
WHERE name = 'double_rare_common'
  AND NOT EXISTS (SELECT 1 FROM pricing_profile_tiers WHERE profile_id = pricing_profiles.id)
UNION ALL
SELECT id, 1.00, NULL, 5.99 FROM pricing_profiles
WHERE name = 'double_rare_common'
  AND NOT EXISTS (SELECT 1 FROM pricing_profile_tiers WHERE profile_id = pricing_profiles.id)
UNION ALL
SELECT id, 0.00, 1.00, 3.99 FROM pricing_profiles
WHERE name = 'double_rare_rh_ur'
  AND NOT EXISTS (SELECT 1 FROM pricing_profile_tiers WHERE profile_id = pricing_profiles.id)
UNION ALL
SELECT id, 1.00, NULL, 4.99 FROM pricing_profiles
WHERE name = 'double_rare_rh_ur'
  AND NOT EXISTS (SELECT 1 FROM pricing_profile_tiers WHERE profile_id = pricing_profiles.id);

COMMIT;
