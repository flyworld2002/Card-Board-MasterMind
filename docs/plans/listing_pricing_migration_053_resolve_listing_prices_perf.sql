-- Migration 053: resolve_listing_prices() perf rewrite + per-function
-- statement_timeout, per Fei's report (2026-09-15) of consistent
-- "canceling statement due to statement timeout" errors loading the
-- Ascended Heroes Reverse Holo - Poke Ball Pattern (Ultra Rare) listing
-- (336458632284, 244-row roster -- one of the largest in the DB).
--
-- Root cause (confirmed via Postgres logs, not guessed): the
-- `authenticated` role's statement_timeout is 8s. The function itself
-- isn't pathologically slow (65ms warm-cache in isolation), but its
-- per-row cost scales with roster size via several correlated
-- subqueries, and it was intermittently colliding with the
-- EbayPullOrders scheduled task (runs every 15 minutes) causing brief
-- I/O pressure -- confirmed via a checkpoint that took 16.6s to flush
-- 165 buffers right before a timeout cluster, vs <1s normally. Large
-- rosters have the least headroom, so they're the first to tip over.
--
-- Two changes, both scoped to resolve_listing_prices() only (not a
-- blanket role-wide timeout bump, which would weaken the timeout
-- protection for every other query):
--
-- 1. Reduced per-row work:
--    - The two separate pricing_profile_tiers lookups (a scalar
--      subquery for the price, a separate EXISTS for matched_a_tier --
--      same predicate evaluated twice) are merged into one
--      LEFT JOIN LATERAL, reused for both.
--    - The qty CTE's two correlated subqueries (inventory sum,
--      platform_listings shared-stock sum -- 2 subquery executions per
--      roster row, 488 for a 244-row listing) are replaced with two
--      GROUP BY-aggregated CTEs (inv_agg, pl_agg) joined once by
--      variant_id -- 2 scans total regardless of roster size.
--    Measured ~15% fewer buffer touches on the two largest listings in
--    the DB (244- and 250-row rosters); wall-clock under warm cache is
--    noise at this scale (both already sub-50ms), the real benefit is
--    less work done per call during the contention windows above.
--
-- 2. ALTER FUNCTION ... SET statement_timeout = '20s' -- a per-function
--    override (Postgres supports pinning a GUC to one function), giving
--    this one expensive read path more room without touching the 8s/3s
--    timeouts that protect every other query in the app.
--
-- Verified before swapping: built the rewrite as a temporary
-- resolve_listing_prices_v2 with the exact same signature, then diffed
-- its output against the live function across every row of every
-- template in the DB (10,375 rows, full coverage) -- zero rows missing
-- on either side, zero differences in resolved_price, price_source,
-- available_qty, total_inventory_qty, low_stock_qty, quantity_limit,
-- market_price, group_name, or derived_label. Only after that came back
-- clean was the live function replaced (CREATE OR REPLACE -- return
-- columns unchanged, so no DROP needed) and the temp v2 dropped.
-- Re-verified post-swap: same 10,375-row total across all templates,
-- and the Ascended Heroes Ultra Rare listing specifically (244 rows,
-- 0 null prices).
--
-- Follow-up hardening (same day, per Fei's push to double-check): the
-- merged tier lookup's LATERAL used LIMIT 1 with no ORDER BY -- safe
-- only because pricing_profile_tiers has a BEFORE INSERT/UPDATE trigger
-- (trg_pricing_tier_no_overlap) rejecting overlapping ranges within a
-- profile, which a live self-join across all 109 rows / 21 profiles
-- confirmed holds true right now (zero overlaps found). That guarantee
-- depends on every write going through normal INSERT/UPDATE though (a
-- COPY or a disabled trigger during some future bulk fix would bypass
-- it), so added an explicit `ORDER BY ppt.min_market DESC, ppt.id LIMIT
-- 1` to make the pick deterministic (narrowest/most-specific matching
-- tier wins) regardless. Mathematically a no-op against current data
-- (there's never more than one candidate row to order between), so this
-- doesn't change any output -- re-verified via the same full-DB check
-- (10,375 rows, 0 null prices) after applying it.

BEGIN;

CREATE OR REPLACE FUNCTION public.resolve_listing_prices(p_platform text, p_listing_id text DEFAULT NULL::text, p_template_id uuid DEFAULT NULL::uuid)
 RETURNS TABLE(row_id uuid, platform_listing_id uuid, card_id uuid, variant_id uuid, card_name text, card_number text, image_url text, derived_label text, set_name text, card_number_numeric integer, status text, group_id uuid, group_name text, market_price numeric, market_price_source text, resolved_price numeric, price_source text, available_qty integer, low_stock_qty integer, quantity_limit integer, manual_price numeric, row_low_stock_qty integer, row_quantity_limit integer, custom_name text, eps_picture_url text, total_inventory_qty integer, card_photo_id uuid)
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
    SELECT l.row_id, mp.market_price, mp.source AS market_price_source
    FROM labeled l
    LEFT JOIN LATERAL (
      SELECT m.market_price, m.source
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
  -- ORDER BY + LIMIT 1: pricing_profile_tiers has a BEFORE INSERT/UPDATE
  -- trigger (trg_pricing_tier_no_overlap) preventing overlapping ranges
  -- within a profile, and a live self-join confirmed zero overlaps exist
  -- today (2026-09-15) -- but LIMIT 1 with no ORDER BY has no guaranteed
  -- tie-break if that ever changes (a COPY, a disabled trigger during a
  -- future bulk fix). Explicit order makes the choice deterministic
  -- (narrowest/most-specific matching tier wins) instead of plan-dependent.
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
      l.status, l.group_id, g.group_name,
      m.market_price, m.market_price_source,
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
    p.total_inventory_qty,
    p.card_photo_id
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

ALTER FUNCTION public.resolve_listing_prices(text, text, uuid) SET statement_timeout = '20s';

COMMIT;
