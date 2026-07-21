-- Migration 002: resolve_listing_prices() RPC
-- Style matches push_staging_row_to_inventory.sql (per CLAUDE.md /
-- repo convention: resolution logic lives in Postgres, not JS/Python, so
-- the web grid and the Python push job can never disagree).
--
-- Resolution order per platform_listings row sharing (p_platform, p_listing_id):
--   1. row.manual_price          -- pin; never overwritten by sync
--   2. most specific matching listing_pricing_rules row -> its profile's
--      tier bracket for the row's market price
--   3. platform default (market * 2 + 1, flagged price_source='default')
--
-- Rule specificity = count of non-null match columns (match_rarity,
-- match_foil_type, match_set_id, match_card_id); ties broken by priority
-- ascending, then newest rule (created_at desc).

CREATE OR REPLACE FUNCTION resolve_listing_prices(p_platform text, p_listing_id text)
RETURNS TABLE (
  row_id uuid,
  card_id uuid,
  variant_id uuid,
  derived_label text,
  market_price numeric,
  resolved_price numeric,
  price_source text,
  available_qty integer,
  low_stock_qty integer
) AS $$
BEGIN
  RETURN QUERY
  WITH base AS (
    SELECT
      pl.id AS row_id,
      pl.manual_price,
      pl.low_stock_qty AS row_low_stock_qty,
      cv.id AS variant_id,
      cv.card_id,
      cv.foil_type,
      cm.rarity,
      cm.set_id,
      ft.display_name AS foil_type_display
    FROM platform_listings pl
    JOIN card_variants cv ON pl.variant_id = cv.id
    JOIN card_master cm ON cv.card_id = cm.id
    LEFT JOIN foil_types ft ON cv.foil_type = ft.code
    WHERE pl.platform = p_platform AND pl.listing_id = p_listing_id
  ),
  matched_rule AS (
    SELECT DISTINCT ON (ranked.row_id)
      ranked.row_id, ranked.rule_id, ranked.profile_id, ranked.rule_low_stock_qty
    FROM (
      SELECT
        b.row_id,
        lpr.id AS rule_id,
        lpr.profile_id,
        lpr.low_stock_qty AS rule_low_stock_qty,
        ( (lpr.match_rarity IS NOT NULL)::int + (lpr.match_foil_type IS NOT NULL)::int
          + (lpr.match_set_id IS NOT NULL)::int + (lpr.match_card_id IS NOT NULL)::int
        ) AS specificity,
        lpr.priority,
        lpr.created_at
      FROM base b
      JOIN listing_pricing_rules lpr
        ON lpr.platform = p_platform AND lpr.listing_id = p_listing_id
        AND (lpr.match_rarity IS NULL OR lpr.match_rarity = b.rarity)
        AND (lpr.match_foil_type IS NULL OR lpr.match_foil_type = b.foil_type)
        AND (lpr.match_set_id IS NULL OR lpr.match_set_id = b.set_id)
        AND (lpr.match_card_id IS NULL OR lpr.match_card_id = b.card_id)
    ) ranked
    ORDER BY ranked.row_id, ranked.specificity DESC, ranked.priority ASC, ranked.created_at DESC
  ),
  market AS (
    SELECT b.row_id, mp.market_price
    FROM base b
    LEFT JOIN LATERAL (
      SELECT m.market_price FROM market_prices m
      WHERE m.variant_id = b.variant_id
      ORDER BY m.updated_at DESC LIMIT 1
    ) mp ON true
  ),
  qty AS (
    SELECT b.row_id,
      (SELECT COALESCE(SUM(i.quantity - i.quantity_sold), 0)::integer
       FROM inventory i WHERE i.variant_id = b.variant_id AND i.is_graded = FALSE
      ) AS available_qty
    FROM base b
  )
  SELECT
    b.row_id,
    b.card_id,
    b.variant_id,
    b.rarity || COALESCE(' ' || b.foil_type_display, '') AS derived_label,
    m.market_price,
    CASE
      WHEN b.manual_price IS NOT NULL THEN b.manual_price
      WHEN mr.profile_id IS NOT NULL THEN (
        SELECT ppt.list_price FROM pricing_profile_tiers ppt
        WHERE ppt.profile_id = mr.profile_id
          AND ppt.min_market <= COALESCE(m.market_price, 0)
          AND (ppt.max_market IS NULL OR COALESCE(m.market_price, 0) < ppt.max_market)
        LIMIT 1
      )
      ELSE ROUND(COALESCE(m.market_price, 0) * 2 + 1, 2)
    END AS resolved_price,
    CASE
      WHEN b.manual_price IS NOT NULL THEN 'pin'
      WHEN mr.rule_id IS NOT NULL THEN 'rule:' || mr.rule_id::text
      ELSE 'default'
    END AS price_source,
    q.available_qty,
    COALESCE(
      b.row_low_stock_qty,
      mr.rule_low_stock_qty,
      (SELECT pp.default_low_stock_qty FROM pricing_profiles pp WHERE pp.id = mr.profile_id)
    ) AS low_stock_qty
  FROM base b
  LEFT JOIN matched_rule mr ON mr.row_id = b.row_id
  LEFT JOIN market m ON m.row_id = b.row_id
  LEFT JOIN qty q ON q.row_id = b.row_id;
END;
$$ LANGUAGE plpgsql STABLE;
