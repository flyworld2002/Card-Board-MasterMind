-- Migration 010: move manual_price/low_stock_qty/quantity_limit off
-- platform_listings entirely, onto listing_card_assignments (the roster
-- row, which exists for every card regardless of status).
--
-- platform_listings is committed to representing live eBay sync state
-- only (external_id, pushed_price/qty/at, sync_enabled) — a queued card
-- has none of that, since it's never been pushed. These three pin columns
-- never belonged there once queued cards needed pins too: a queued row
-- has no platform_listings row at all, so there was nowhere to store a
-- pin for it under the old design. Rather than duplicate the columns in
-- both places (drift risk — which one wins?), they move to
-- listing_card_assignments as the single source of truth for every
-- roster row, active or queued. On promotion (queued -> active), nothing
-- needs to be copied: it's the same listing_card_assignments.id before
-- and after, so a pin set while queued just keeps applying once live.
--
-- Confirmed zero live data before dropping: no platform_listings row had
-- a non-null manual_price, low_stock_qty, or quantity_limit at the time
-- of this migration (verified via COUNT(*) FILTER — all 0), so this is a
-- clean move, not a lossy one.
--
-- Known side effect (flagged, not blocking): importer/ebay_listing_sync.py's
-- OLD, already-superseded --ebay-recalc-prices pipeline (_apply_bumps)
-- reads listing.get("quantity_limit") off a `SELECT pl.*` on
-- platform_listings for its low-stock-bump feature. After this migration
-- that key is simply absent, so .get() silently returns None instead of
-- erroring — low_stock_bump becomes permanently inert on that pipeline
-- (no crash, just quietly stops firing). --ebay-recalc-prices was already
-- documented as superseded by this Listing Pricing System and not
-- actively used going forward; not fixing it up to keep parity with a
-- pipeline this project is deliberately moving away from.

ALTER TABLE listing_card_assignments ADD COLUMN manual_price numeric;
ALTER TABLE listing_card_assignments ADD COLUMN low_stock_qty integer;
ALTER TABLE listing_card_assignments ADD COLUMN quantity_limit integer;

ALTER TABLE platform_listings DROP COLUMN manual_price;
ALTER TABLE platform_listings DROP COLUMN low_stock_qty;
ALTER TABLE platform_listings DROP COLUMN quantity_limit;

DROP FUNCTION IF EXISTS resolve_listing_prices(text, text);

CREATE OR REPLACE FUNCTION resolve_listing_prices(p_platform text, p_listing_id text)
RETURNS TABLE (
  row_id uuid,
  platform_listing_id uuid,
  card_id uuid,
  variant_id uuid,
  card_name text,
  card_number text,
  image_url text,
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
  quantity_limit integer,
  manual_price numeric,
  row_low_stock_qty integer,
  row_quantity_limit integer
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
      cm.name AS card_name,
      cm.card_number,
      COALESCE(cm.image_url_own, cm.image_url) AS image_url,
      cm.rarity,
      cm.card_number_numeric,
      cs.name AS set_name,
      lca.manual_price,
      lca.low_stock_qty AS row_low_stock_qty,
      lca.quantity_limit AS row_quantity_limit
    FROM listing_card_assignments lca
    JOIN card_variants cv ON lca.variant_id = cv.id
    JOIN card_master cm ON cv.card_id = cm.id
    JOIN card_sets cs ON cm.set_id = cs.id
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
      l.card_name, l.card_number, l.image_url,
      l.rarity || COALESCE(' ' || l.foil_type_display, '') AS derived_label,
      l.set_name, l.card_number_numeric, l.status, l.group_id, g.group_name,
      m.market_price,
      l.manual_price,
      l.row_low_stock_qty,
      l.row_quantity_limit,
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
    p.quantity_limit,
    p.manual_price,
    p.row_low_stock_qty,
    p.row_quantity_limit
  FROM priced p
  ORDER BY p.set_name, p.card_number_numeric;
END;
$$ LANGUAGE plpgsql STABLE;
