-- Migration 054: previous_market_price — lets the Listing Pricing UI show
-- "was $X, now $Y (+N%)" under the Market column (Fei, 2026-10-26 request
-- #1). market_prices was upsert-only (upsert_market_price() in
-- db/connection.py) — every refresh overwrote the row in place, so there
-- was never anywhere to read a "previous" value from. This shifts the
-- current value into previous_market_price/previous_updated_at right
-- before it gets overwritten, but ONLY when the price actually changed
-- (an unchanged refresh must not churn "previous" into "same as current").
--
-- History starts from whenever this ships, not retroactively — no past
-- price data existed to backfill from.

BEGIN;

ALTER TABLE market_prices
  ADD COLUMN previous_market_price numeric,
  ADD COLUMN previous_updated_at timestamptz;

CREATE OR REPLACE FUNCTION public.resolve_listing_prices(p_platform text, p_listing_id text DEFAULT NULL::text, p_template_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(row_id uuid, platform_listing_id uuid, card_id uuid, variant_id uuid, card_name text, card_number text, image_url text, derived_label text, set_name text, card_number_numeric integer, status text, group_id uuid, group_name text, market_price numeric, market_price_source text, previous_market_price numeric, resolved_price numeric, price_source text, available_qty integer, low_stock_qty integer, quantity_limit integer, manual_price numeric, row_low_stock_qty integer, row_quantity_limit integer, custom_name text, eps_picture_url text, total_inventory_qty integer, card_photo_id uuid)
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
  v_template_id uuid;
  v_base_price numeric;
  v_default_quantity_limit integer;
  v_display_sort text;
BEGIN
  IF p_template_id IS NOT NULL THEN
    SELECT id, base_price, default_quantity_limit, display_sort
      INTO v_template_id, v_base_price, v_default_quantity_limit, v_display_sort
    FROM listing_templates
    WHERE id = p_template_id AND platform = p_platform;
  ELSE
    SELECT id, base_price, default_quantity_limit, display_sort
      INTO v_template_id, v_base_price, v_default_quantity_limit, v_display_sort
    FROM listing_templates
    WHERE platform = p_platform AND listing_id = p_listing_id;
  END IF;

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
      lca.priority_rank,
      cv.card_id,
      cv.foil_type,
      cv.foil_pattern,
      cv.texture,
      cv.material,
      cv.size,
      cv.stamp_type,
      cv.source_type,
      cm.name AS card_name,
      cm.card_number,
      COALESCE(cm.image_url_own, cm.image_url) AS image_url,
      cm.rarity,
      cm.card_number_numeric,
      cs.name AS set_name,
      cs.release_year,
      lca.manual_price,
      lca.low_stock_qty AS row_low_stock_qty,
      lca.quantity_limit AS row_quantity_limit,
      lca.custom_name,
      lca.eps_picture_url,
      lca.card_photo_id
    FROM listing_card_assignments lca
    JOIN card_variants cv ON lca.variant_id = cv.id
    JOIN card_master cm ON cv.card_id = cm.id
    JOIN card_sets cs ON cm.set_id = cs.id
    WHERE lca.template_id = v_template_id
  ),
  labeled AS (
    SELECT b.*,
           ft.display_name AS foil_type_display,
           fp.display_name AS foil_pattern_display,
           tx.display_name AS texture_display,
           mt.display_name AS material_display,
           COALESCE(r.sort_order, 999999) AS rarity_sort_order
    FROM base b
    LEFT JOIN foil_types ft ON b.foil_type = ft.code
    LEFT JOIN foil_patterns fp ON b.foil_pattern = fp.code
    LEFT JOIN textures tx ON b.texture = tx.code
    LEFT JOIN materials mt ON b.material = mt.code
    LEFT JOIN rarities r ON b.rarity = r.code
  ),
  grp AS (
    SELECT l.row_id, lcg.name AS group_name, lcg.profile_id, pp2.round_to AS profile_round_to
    FROM labeled l
    LEFT JOIN listing_card_groups lcg ON lcg.id = l.group_id
    LEFT JOIN pricing_profiles pp2 ON pp2.id = lcg.profile_id
  ),
  market AS (
    SELECT l.row_id, mp.market_price, mp.source AS market_price_source, mp.previous_market_price
    FROM labeled l
    LEFT JOIN LATERAL (
      SELECT m.market_price, m.source, m.previous_market_price
      FROM market_prices m
      WHERE m.variant_id = l.variant_id
      ORDER BY m.updated_at DESC LIMIT 1
    ) mp ON true
  ),
  inv_agg AS (
    SELECT i.variant_id, SUM(i.quantity - i.quantity_sold)::integer AS total_inventory_qty
    FROM inventory i
    WHERE i.is_graded = FALSE
    GROUP BY i.variant_id
  ),
  pl_agg AS (
    SELECT pl2.variant_id, SUM(pl2.quantity_listed)::integer AS listed_elsewhere_qty
    FROM platform_listings pl2
    WHERE pl2.platform = p_platform
      AND pl2.status = 'active'
      AND (p_listing_id IS NULL OR pl2.listing_id <> p_listing_id)
    GROUP BY pl2.variant_id
  ),
  qty AS (
    SELECT l.row_id,
      COALESCE(ia.total_inventory_qty, 0) AS total_inventory_qty,
      GREATEST(COALESCE(ia.total_inventory_qty, 0) - COALESCE(pa.listed_elsewhere_qty, 0), 0) AS available_qty
    FROM labeled l
    LEFT JOIN inv_agg ia ON ia.variant_id = l.variant_id
    LEFT JOIN pl_agg pa ON pa.variant_id = l.variant_id
  ),
  tier AS (
    SELECT l.row_id, t.list_price, t.multiplier, t.plus, t.tier_found
    FROM labeled l
    LEFT JOIN grp g ON g.row_id = l.row_id
    LEFT JOIN market m ON m.row_id = l.row_id
    LEFT JOIN LATERAL (
      SELECT ppt.list_price, ppt.multiplier, ppt.plus, true AS tier_found
      FROM pricing_profile_tiers ppt
      WHERE ppt.profile_id = g.profile_id
        AND ppt.min_market <= COALESCE(m.market_price, 0)
        AND (ppt.max_market IS NULL OR COALESCE(m.market_price, 0) < ppt.max_market)
      ORDER BY ppt.min_market DESC, ppt.id
      LIMIT 1
    ) t ON true
  ),
  priced AS (
    SELECT
      l.row_id, l.platform_listing_id, l.card_id, l.variant_id,
      l.card_name, l.card_number, l.image_url,
      l.rarity || COALESCE(' ' || l.foil_type_display, '') ||
        COALESCE(' · ' || NULLIF(array_to_string(
          ARRAY[l.foil_pattern_display, l.texture_display, l.material_display,
                l.size, l.stamp_type, l.source_type],
          ' · '
        ), ''), '') AS derived_label,
      l.set_name, l.card_number_numeric, l.release_year, l.rarity_sort_order,
      l.priority_rank,
      l.status, l.group_id, g.group_name,
      m.market_price, m.market_price_source, m.previous_market_price,
      l.manual_price,
      l.row_low_stock_qty,
      l.row_quantity_limit,
      l.custom_name,
      l.eps_picture_url,
      l.card_photo_id,
      COALESCE(
        CASE
          WHEN t.tier_found IS NOT TRUE THEN NULL
          WHEN t.list_price IS NOT NULL THEN t.list_price
          ELSE apply_price_rounding(
                 ROUND(COALESCE(m.market_price, 0) * t.multiplier + COALESCE(t.plus, 0), 2),
                 g.profile_round_to)
        END,
        ROUND(COALESCE(m.market_price, 0) * 2 + 1, 2)
      ) AS non_pin_price,
      COALESCE(t.tier_found, false) AS matched_a_tier,
      q.available_qty,
      q.total_inventory_qty,
      COALESCE(
        l.row_low_stock_qty,
        (SELECT pp.default_low_stock_qty FROM pricing_profiles pp WHERE pp.id = g.profile_id)
      ) AS low_stock_qty,
      COALESCE(
        l.row_quantity_limit,
        (SELECT pp.default_quantity_limit FROM pricing_profiles pp WHERE pp.id = g.profile_id),
        v_default_quantity_limit,
        24
      ) AS quantity_limit
    FROM labeled l
    LEFT JOIN grp g ON g.row_id = l.row_id
    LEFT JOIN market m ON m.row_id = l.row_id
    LEFT JOIN qty q ON q.row_id = l.row_id
    LEFT JOIN tier t ON t.row_id = l.row_id
  )
  SELECT
    p.row_id, p.platform_listing_id, p.card_id, p.variant_id,
    p.card_name, p.card_number, p.image_url, p.derived_label,
    p.set_name, p.card_number_numeric, p.status, p.group_id, p.group_name,
    p.market_price, p.market_price_source, p.previous_market_price,
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
    p.quantity_limit,
    p.manual_price,
    p.row_low_stock_qty,
    p.row_quantity_limit,
    p.custom_name,
    p.eps_picture_url,
    p.total_inventory_qty,
    p.card_photo_id
  FROM priced p
  ORDER BY
    CASE WHEN v_display_sort = 'manual' THEN p.priority_rank END,
    CASE WHEN v_display_sort = 'alpha' THEN p.card_name END,
    CASE WHEN v_display_sort = 'number' THEN p.card_number_numeric END,
    CASE WHEN v_display_sort = 'release_date' THEN p.release_year END,
    CASE WHEN v_display_sort = 'release_date' THEN p.set_name END,
    CASE WHEN v_display_sort = 'release_date' THEN p.card_number_numeric END,
    CASE WHEN v_display_sort = 'rarity' THEN p.rarity_sort_order END,
    CASE WHEN v_display_sort = 'rarity' THEN p.card_number_numeric END,
    p.set_name, p.card_number_numeric;
END;
$function$;

COMMIT;
