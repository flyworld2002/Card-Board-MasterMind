-- Migration 043: default_card_photo_ids() — batch wrapper around
-- default_card_photo_id() (migration 041), same pattern as
-- render_variation_names() wrapping render_variation_name() (migration
-- 037). "+ Batch add cards" can resolve default photos for every
-- checked candidate in one round trip instead of one RPC call per card.

BEGIN;

CREATE OR REPLACE FUNCTION default_card_photo_ids(p_variant_ids uuid[], p_target_finish_kind text DEFAULT NULL)
RETURNS TABLE(variant_id uuid, card_photo_id uuid) AS $$
  SELECT vid, default_card_photo_id(vid, p_target_finish_kind) FROM unnest(p_variant_ids) AS vid;
$$ LANGUAGE sql STABLE;

COMMIT;
