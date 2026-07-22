-- Migration 006: respect listing_templates.base_price (floor) and
-- default_quantity_limit (qty cap) in resolve_listing_prices(), which
-- were both real gaps — columns existed and were documented as
-- "raise-only floor" / qty cap but never actually consulted anywhere.
--
-- Floor: applied to group/default-resolved prices only, NOT to pins —
-- a pin is an explicit human decision and shouldn't be second-guessed by
-- a safety-net floor (matches the original locked design's "floors
-- skipped when price came from an absolute override").
--
-- Quantity limit: new `quantity_limit` output column, resolved as
-- COALESCE(listing_templates.default_quantity_limit,
-- platform_listings.quantity_limit, 24) — template default WINS when set.
-- Confirmed live: ALL 9363 platform_listings rows show quantity_limit=18,
-- the column's blanket default from an earlier migration — never a real
-- per-card override — so it can't be trusted to take precedence over an
-- actually-configured template default; only used as a fallback when the
-- template has none. available_qty stays the TRUE inventory count
-- (unchanged meaning); the cap is applied at push time in
-- ebay_pushprices.py, same pattern as low_stock_qty gating.
--
-- Also made the profile-tier lookup fall back to the platform-default
-- formula instead of returning NULL when a profile has no matching tier
-- (e.g. a brand new profile created inline with no tiers yet) — a real
-- edge case now more likely given profiles can be created directly from
-- the Listing pricing page.

DROP FUNCTION IF EXISTS resolve_listing_prices(text, text);

CREATE OR REPLACE FUNCTION resolve_listing_prices(p_platform text, p_listing_id text)
RETURNS TABLE (
  row_id uuid,
  platform_listing_id uuid,
  card_id uuid,
  variant_id uuid,
  derived_label text,
  set_name text,
  card_number_numeric integer,
  status text,
  group_id uuid,
  group_name text,
  market_price numeric,
  resolved_price numeric,
  price_source text,
  available_qty integer,
  low_stock_qty integer,
  quantity_limit integer
) AS $$
DECLARE
  v_template_id uuid;
  v_base_price numeric;
  v_default_quantity_limit integer;
BEGIN
  SELECT id, base_price, default_quantity_limit
    INTO v_template_id, v_base_price, v_default_quantity_limit
  FROM listing_templates
  WHERE platform = p_platform AND listing_id = p_listing_id;

  IF v_template_id IS NULL THEN
    RETURN;
  END IF;

  RETURN QUERY
  WITH base AS (
    SELECT
      lca.id AS row_id,
      lca.platform_listing_id,
      lca.status,
      lca.group_id,
      lca.variant_id,
      cv.card_id,
      cv.foil_type,
      cm.rarity,
      cm.card_number_numeric,
      cs.name AS set_name,
      pl.manual_price,
      pl.low_stock_qty AS row_low_stock_qty,
      pl.quantity_limit AS row_quantity_limit
    FROM listing_card_assignments lca
    JOIN card_variants cv ON lca.variant_id = cv.id
    JOIN card_master cm ON cv.card_id = cm.id
    JOIN card_sets cs ON cm.set_id = cs.id
    LEFT JOIN platform_listings pl ON pl.id = lca.platform_listing_id
    WHERE lca.template_id = v_template_id
  ),
  labeled AS (
    SELECT b.*, ft.display_name AS foil_type_display
    FROM base b
    LEFT JOIN foil_types ft ON b.foil_type = ft.code
  ),
  grp AS (
    SELECT l.row_id, lcg.name AS group_name, lcg.profile_id
    FROM labeled l
    LEFT JOIN listing_card_groups lcg ON lcg.id = l.group_id
  ),
  market AS (
    SELECT l.row_id, mp.market_price
    FROM labeled l
    LEFT JOIN LATERAL (
      SELECT m.market_price FROM market_prices m
      WHERE m.variant_id = l.variant_id
      ORDER BY m.updated_at DESC LIMIT 1
    ) mp ON true
  ),
  qty AS (
    SELECT l.row_id,
      (SELECT COALESCE(SUM(i.quantity - i.quantity_sold), 0)::integer
       FROM inventory i WHERE i.variant_id = l.variant_id AND i.is_graded = FALSE
      ) AS available_qty
    FROM labeled l
  ),
  priced AS (
    SELECT
      l.row_id, l.platform_listing_id, l.card_id, l.variant_id,
      l.rarity || COALESCE(' ' || l.foil_type_display, '') AS derived_label,
      l.set_name, l.card_number_numeric, l.status, l.group_id, g.group_name,
      m.market_price,
      l.manual_price,
      -- price before the floor: pin -> tier (or default formula if the
      -- profile has no matching tier) -> default formula
      COALESCE(
        (
          SELECT ppt.list_price FROM pricing_profile_tiers ppt
          WHERE ppt.profile_id = g.profile_id
            AND ppt.min_market <= COALESCE(m.market_price, 0)
            AND (ppt.max_market IS NULL OR COALESCE(m.market_price, 0) < ppt.max_market)
          LIMIT 1
        ),
        ROUND(COALESCE(m.market_price, 0) * 2 + 1, 2)
      ) AS non_pin_price,
      (g.profile_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM pricing_profile_tiers ppt
        WHERE ppt.profile_id = g.profile_id
          AND ppt.min_market <= COALESCE(m.market_price, 0)
          AND (ppt.max_market IS NULL OR COALESCE(m.market_price, 0) < ppt.max_market)
      )) AS matched_a_tier,
      q.available_qty,
      COALESCE(
        l.row_low_stock_qty,
        (SELECT pp.default_low_stock_qty FROM pricing_profiles pp WHERE pp.id = g.profile_id)
      ) AS low_stock_qty,
      COALESCE(v_default_quantity_limit, l.row_quantity_limit, 24) AS quantity_limit
    FROM labeled l
    LEFT JOIN grp g ON g.row_id = l.row_id
    LEFT JOIN market m ON m.row_id = l.row_id
    LEFT JOIN qty q ON q.row_id = l.row_id
  )
  SELECT
    p.row_id, p.platform_listing_id, p.card_id, p.variant_id, p.derived_label,
    p.set_name, p.card_number_numeric, p.status, p.group_id, p.group_name,
    p.market_price,
    CASE
      WHEN p.manual_price IS NOT NULL THEN p.manual_price
      WHEN v_base_price IS NOT NULL THEN GREATEST(p.non_pin_price, v_base_price)
      ELSE p.non_pin_price
    END AS resolved_price,
    CASE
      WHEN p.manual_price IS NOT NULL THEN 'pin'
      WHEN p.group_id IS NOT NULL AND p.matched_a_tier THEN 'group:' || p.group_id::text
      ELSE 'default'
    END AS price_source,
    p.available_qty,
    p.low_stock_qty,
    p.quantity_limit
  FROM priced p
  ORDER BY p.set_name, p.card_number_numeric;
END;
$$ LANGUAGE plpgsql STABLE;
