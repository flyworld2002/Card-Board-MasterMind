-- Migration 005: add set_name + card_number_numeric to
-- resolve_listing_prices() so the web grid can sort ungrouped (and any)
-- cards by Set then card number, per Fei's request. Pure display/sort
-- data, no change to resolution logic.

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
  low_stock_qty integer
) AS $$
DECLARE
  v_template_id uuid;
BEGIN
  SELECT id INTO v_template_id FROM listing_templates
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
      pl.low_stock_qty AS row_low_stock_qty
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
  )
  SELECT
    l.row_id,
    l.platform_listing_id,
    l.card_id,
    l.variant_id,
    l.rarity || COALESCE(' ' || l.foil_type_display, '') AS derived_label,
    l.set_name,
    l.card_number_numeric,
    l.status,
    l.group_id,
    g.group_name,
    m.market_price,
    CASE
      WHEN l.manual_price IS NOT NULL THEN l.manual_price
      WHEN g.profile_id IS NOT NULL THEN (
        SELECT ppt.list_price FROM pricing_profile_tiers ppt
        WHERE ppt.profile_id = g.profile_id
          AND ppt.min_market <= COALESCE(m.market_price, 0)
          AND (ppt.max_market IS NULL OR COALESCE(m.market_price, 0) < ppt.max_market)
        LIMIT 1
      )
      ELSE ROUND(COALESCE(m.market_price, 0) * 2 + 1, 2)
    END AS resolved_price,
    CASE
      WHEN l.manual_price IS NOT NULL THEN 'pin'
      WHEN g.profile_id IS NOT NULL THEN 'group:' || l.group_id::text
      ELSE 'default'
    END AS price_source,
    q.available_qty,
    COALESCE(
      l.row_low_stock_qty,
      (SELECT pp.default_low_stock_qty FROM pricing_profiles pp WHERE pp.id = g.profile_id)
    ) AS low_stock_qty
  FROM labeled l
  LEFT JOIN grp g ON g.row_id = l.row_id
  LEFT JOIN market m ON m.row_id = l.row_id
  LEFT JOIN qty q ON q.row_id = l.row_id
  ORDER BY l.set_name, l.card_number_numeric;
END;
$$ LANGUAGE plpgsql STABLE;
