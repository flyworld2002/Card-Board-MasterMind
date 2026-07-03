-- ============================================================
-- push_staging_row_to_inventory(staging_id uuid)
--
-- Ports the logic from staging_workflow.py's _push_batch_by_order
-- for a SINGLE staging row. Designed to be called via:
--   supabase.rpc('push_staging_row_to_inventory', { staging_id: '...' })
--
-- Behavior:
--   1. Validates the staging row is push-ready (card_id set, status allows push)
--   2. Resolves/creates the purchases row by reference_id = order_number
--   3. Resolves/creates the card_variant (IS NOT DISTINCT FROM lookup)
--   4. Inserts card_variant_sources row if source_type present
--   5. Inserts the inventory row (cost/asking split by source)
--   6. Upserts market_prices if price data is available
--   7. Marks the staging row as 'processed'
--
-- Returns: jsonb summary { inventory_id, variant_id, purchase_id }
-- Raises an exception (rolls back) on any validation failure.
-- ============================================================

CREATE OR REPLACE FUNCTION push_staging_row_to_inventory(p_staging_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_row            staging%ROWTYPE;
    v_purchase_id    uuid;
    v_variant_id     uuid;
    v_inventory_id   uuid;
    v_variant_type   text;
    v_finish         text;
    v_is_special     boolean;
    v_is_ebay        boolean;
    v_cost           numeric;
    v_asking         numeric;
    v_list_price     numeric;
    v_market_price   numeric;
    v_market_date    date;
    v_special_patterns text[] := ARRAY[
        'Cosmos Holo', 'Master Ball Pattern', 'Poke Ball Pattern',
        'Cracked Ice Holo', 'Galaxy Holo'
    ];
BEGIN
    -- ── 1. Load and validate the staging row ────────────────────────────
    SELECT * INTO v_row FROM staging WHERE id = p_staging_id FOR UPDATE;

    IF v_row IS NULL THEN
        RAISE EXCEPTION 'Staging row % not found', p_staging_id;
    END IF;

    IF v_row.card_id IS NULL THEN
        RAISE EXCEPTION 'Staging row % has no card_id — resolve the match before pushing', p_staging_id;
    END IF;

    IF v_row.status = 'processed' THEN
        RAISE EXCEPTION 'Staging row % was already processed', p_staging_id;
    END IF;

    v_is_ebay := (v_row.source = 'ebay');

    -- ── 2. Resolve or create the purchase ────────────────────────────────
    SELECT id INTO v_purchase_id
    FROM purchases
    WHERE reference_id = v_row.order_number;

    IF v_purchase_id IS NULL THEN
        -- Initial values are placeholders only — step 5b recomputes
        -- card_count/total_cost/purchase_type from actual inventory rows
        -- immediately after, so these never go stale.
        INSERT INTO purchases (
            source, purchase_type, reference_id, total_cost,
            card_count, purchased_at
        ) VALUES (
            COALESCE(v_row.source, 'tcgplayer'),
            'single',
            v_row.order_number,
            v_row.price * v_row.quantity,
            v_row.quantity,
            COALESCE(v_row.order_date, now())
        )
        RETURNING id INTO v_purchase_id;
    END IF;

    -- ── 3. Resolve or create the card_variant ────────────────────────────
    v_variant_type := COALESCE(v_row.foil_pattern, v_row.foil_type, 'Non-Holo');
    v_finish       := COALESCE(v_row.foil_type, 'Non-Holo');
    v_is_special   := v_variant_type = ANY(v_special_patterns);

    SELECT id INTO v_variant_id
    FROM card_variants
    WHERE card_id = v_row.card_id
      AND variant_type = v_variant_type
      AND finish = v_finish
      AND source_type IS NOT DISTINCT FROM v_row.source_type
      AND stamp_type  IS NOT DISTINCT FROM v_row.stamp_type;

    IF v_variant_id IS NULL THEN
        INSERT INTO card_variants (
            card_id, variant_type, finish, is_special, source_type, stamp_type
        ) VALUES (
            v_row.card_id, v_variant_type, v_finish, v_is_special,
            v_row.source_type, v_row.stamp_type
        )
        ON CONFLICT (card_id, variant_type, finish, source_type, stamp_type)
        DO UPDATE SET is_special = EXCLUDED.is_special
        RETURNING id INTO v_variant_id;
    END IF;

    -- ── 4. Record variant source if present (e.g. deck_exclusive) ────────
    IF v_row.source_type IS NOT NULL THEN
        INSERT INTO card_variant_sources (variant_id, source_type, product_name)
        VALUES (v_variant_id, v_row.source_type, v_row.notes)
        ON CONFLICT (variant_id, source_type, product_name) DO NOTHING;
    END IF;

    -- ── 5. Insert the inventory row ──────────────────────────────────────
    v_list_price := COALESCE(v_row.override_price, v_row.calculated_price);

    IF v_is_ebay THEN
        v_cost   := 0;
        v_asking := v_row.price;
    ELSE
        v_cost   := v_row.price;
        v_asking := v_list_price;
    END IF;

    INSERT INTO inventory (
        card_id, purchase_id, condition, is_graded, quantity,
        cost_basis, asking_price, notes, acquired_at, variant_id
    ) VALUES (
        v_row.card_id, v_purchase_id, v_row.condition, false, v_row.quantity,
        v_cost, v_asking, v_row.notes,
        COALESCE(v_row.order_date, now()), v_variant_id
    )
    RETURNING id INTO v_inventory_id;

    -- ── 5b. Recompute purchase summary totals ────────────────────────────
    -- Reflects ALL inventory rows linked to this purchase, not just this one,
    -- so card_count/total_cost/purchase_type stay accurate as additional
    -- staging rows from the same order are pushed over time.
    UPDATE purchases p
    SET
        card_count    = totals.card_count,
        total_cost    = totals.total_cost,
        purchase_type = CASE WHEN totals.card_count > 1 THEN 'lot' ELSE 'single' END
    FROM (
        SELECT
            COALESCE(SUM(quantity), 0)              AS card_count,
            COALESCE(SUM(cost_basis * quantity), 0) AS total_cost
        FROM inventory
        WHERE purchase_id = v_purchase_id
    ) AS totals
    WHERE p.id = v_purchase_id;

    -- ── 6. Upsert market price if available ──────────────────────────────
    -- For eBay rows: staging.market_price / staging.market_price_date columns.
    -- For TCGPlayer rows: market price was already written during staging
    -- import (tcgplayer_html.py), so this is a best-effort top-up only.
    v_market_price := v_row.market_price;
    v_market_date  := v_row.market_price_date;

    IF v_market_price IS NOT NULL THEN
        INSERT INTO market_prices (variant_id, condition, market_price, source, updated_at)
        VALUES (
            v_variant_id, v_row.condition, v_market_price,
            CASE WHEN v_is_ebay THEN 'ebay' ELSE 'pokemontcg' END,
            COALESCE(v_market_date::timestamptz, now())
        )
        ON CONFLICT (variant_id, condition)
        DO UPDATE SET
            market_price = EXCLUDED.market_price,
            source       = EXCLUDED.source,
            updated_at   = EXCLUDED.updated_at
        WHERE market_prices.updated_at < EXCLUDED.updated_at;
    END IF;

    -- ── 7. Mark staging row as processed ────────────────────────────────
    UPDATE staging
    SET status = 'processed', updated_at = now()
    WHERE id = p_staging_id;

    RETURN jsonb_build_object(
        'inventory_id', v_inventory_id,
        'variant_id',   v_variant_id,
        'purchase_id',  v_purchase_id
    );
END;
$$;