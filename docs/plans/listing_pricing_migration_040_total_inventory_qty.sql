-- Migration 040: resolve_listing_prices() returns total_inventory_qty
-- alongside available_qty. Fei wants the roster table's "Available"
-- column to show "available/total" (e.g. "0/5") so a card that's fully
-- claimed by other listings (available_qty=0) but still owned in stock
-- is visible at a glance as a rebalancing candidate, without needing to
-- open the "Balance" modal first to discover it.
--
-- total_inventory_qty is the same raw SUM(quantity - quantity_sold)
-- openBalanceQtyModal() (listing-pricing.js) already computes
-- client-side when you DO open that modal — just now surfaced inline
-- too, computed once via LATERAL (same pattern the market CTE already
-- uses) and reused for both columns rather than querying inventory
-- twice.

BEGIN;

-- Postgres won't let CREATE OR REPLACE change a RETURNS TABLE function's
-- output columns at all, even appending one at the end — confirmed live
-- this session ("cannot change return type of existing function").
-- Confirmed no other function/view depends on this one (pg_depend) before
-- dropping.
DROP FUNCTION IF EXISTS resolve_listing_prices(text, text, uuid);

CREATE FUNCTION resolve_listing_prices(p_platform text, p_listing_id text DEFAULT NULL::text, p_template_id uuid DEFAULT NULL::uuid)
RETURNS TABLE(row_id uuid, platform_listing_id uuid, card_id uuid, variant_id uuid, card_name text, card_number text, image_url text, derived_label text, set_name text, card_number_numeric integer, status text, group_id uuid, group_name text, market_price numeric, market_price_source text, resolved_price numeric, price_source text, available_qty integer, low_stock_qty integer, quantity_limit integer, manual_price numeric, row_low_stock_qty integer, row_quantity_limit integer, custom_name text, eps_picture_url text, total_inventory_qty integer)
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
      cv.card_id,
      cv.foil_type,
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
      lca.eps_picture_url
    FROM listing_card_assignments lca
    JOIN card_variants cv ON lca.variant_id = cv.id
    JOIN card_master cm ON cv.card_id = cm.id
    JOIN card_sets cs ON cm.set_id = cs.id
    WHERE lca.template_id = v_template_id
  ),
  labeled AS (
    SELECT b.*, ft.display_name AS foil_type_display, COALESCE(r.sort_order, 999999) AS rarity_sort_order
    FROM base b
    LEFT JOIN foil_types ft ON b.foil_type = ft.code
    LEFT JOIN rarities r ON b.rarity = r.code
  ),
  grp AS (
    SELECT l.row_id, lcg.name AS group_name, lcg.profile_id
    FROM labeled l
    LEFT JOIN listing_card_groups lcg ON lcg.id = l.group_id
  ),
  market AS (
    SELECT l.row_id, mp.market_price, mp.source AS market_price_source
    FROM labeled l
    LEFT JOIN LATERAL (
      SELECT m.market_price, m.source
      FROM market_prices m
      WHERE m.variant_id = l.variant_id
      ORDER BY m.updated_at DESC LIMIT 1
    ) mp ON true
  ),
  qty AS (
    SELECT l.row_id,
      inv.total_inventory_qty,
      GREATEST(
        inv.total_inventory_qty
        -
        (SELECT COALESCE(SUM(pl2.quantity_listed), 0)::integer
         FROM platform_listings pl2
         WHERE pl2.variant_id = l.variant_id
           AND pl2.platform = p_platform
           AND pl2.status = 'active'
           AND (p_listing_id IS NULL OR pl2.listing_id <> p_listing_id)
        ),
        0
      ) AS available_qty
    FROM labeled l
    CROSS JOIN LATERAL (
      SELECT COALESCE(SUM(i.quantity - i.quantity_sold), 0)::integer AS total_inventory_qty
      FROM inventory i WHERE i.variant_id = l.variant_id AND i.is_graded = FALSE
    ) inv
  ),
  priced AS (
    SELECT
      l.row_id, l.platform_listing_id, l.card_id, l.variant_id,
      l.card_name, l.card_number, l.image_url,
      l.rarity || COALESCE(' ' || l.foil_type_display, '') AS derived_label,
      l.set_name, l.card_number_numeric, l.release_year, l.rarity_sort_order,
      l.status, l.group_id, g.group_name,
      m.market_price, m.market_price_source,
      l.manual_price,
      l.row_low_stock_qty,
      l.row_quantity_limit,
      l.custom_name,
      l.eps_picture_url,
      COALESCE(
        (
          SELECT CASE
                   WHEN ppt.list_price IS NOT NULL THEN ppt.list_price
                   ELSE ROUND(COALESCE(m.market_price, 0) * ppt.multiplier + COALESCE(ppt.plus, 0), 2)
                 END
          FROM pricing_profile_tiers ppt
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
      q.total_inventory_qty,
      COALESCE(
        l.row_low_stock_qty,
        (SELECT pp.default_low_stock_qty FROM pricing_profiles pp WHERE pp.id = g.profile_id)
      ) AS low_stock_qty,
      COALESCE(l.row_quantity_limit, v_default_quantity_limit, 24) AS quantity_limit
    FROM labeled l
    LEFT JOIN grp g ON g.row_id = l.row_id
    LEFT JOIN market m ON m.row_id = l.row_id
    LEFT JOIN qty q ON q.row_id = l.row_id
  )
  SELECT
    p.row_id, p.platform_listing_id, p.card_id, p.variant_id,
    p.card_name, p.card_number, p.image_url, p.derived_label,
    p.set_name, p.card_number_numeric, p.status, p.group_id, p.group_name,
    p.market_price, p.market_price_source,
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
    p.total_inventory_qty
  FROM priced p
  ORDER BY
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
