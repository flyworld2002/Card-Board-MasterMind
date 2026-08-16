-- Migration 034: search_roster_candidates() gets an explicit p_limit.
--
-- Same class of bug as migration 033: an unfiltered/loosely-filtered call
-- returns up to 9,422 rows (card_variants row count) today, well past
-- Supabase/PostgREST's default 1,000-row REST response cap — the "Batch
-- add cards" preview would silently show a truncated, arbitrary-order
-- subset with no indication anything was cut off. Rather than relying on
-- the client to ever hit that invisible cap, the RPC now caps itself
-- explicitly and the JS side treats "got exactly p_limit rows back" as
-- "there may be more — narrow your filters" rather than "this is
-- everything." Default (500) comfortably covers a normal themed-listing
-- batch-add (the Mega Evolution ex case that motivated this whole
-- feature is ~76 rows) while keeping an unfiltered/too-broad query from
-- ever silently truncating without saying so.

BEGIN;

DROP FUNCTION IF EXISTS search_roster_candidates(
  uuid, text[], uuid[], text[], text[], text[], text[], text[], text[], text[], text[], text[], text[], boolean
);

CREATE OR REPLACE FUNCTION search_roster_candidates(
  p_template_id uuid,
  p_series text[] DEFAULT NULL,
  p_set_ids uuid[] DEFAULT NULL,
  p_name_terms text[] DEFAULT NULL,
  p_related_pokemon text[] DEFAULT NULL,
  p_rarities text[] DEFAULT NULL,
  p_foil_types text[] DEFAULT NULL,
  p_foil_patterns text[] DEFAULT NULL,
  p_textures text[] DEFAULT NULL,
  p_materials text[] DEFAULT NULL,
  p_sizes text[] DEFAULT NULL,
  p_stamp_types text[] DEFAULT NULL,
  p_source_types text[] DEFAULT NULL,
  p_exclude_secret_rare boolean DEFAULT true,
  p_limit integer DEFAULT 500
)
RETURNS TABLE (
  variant_id uuid,
  card_id uuid,
  card_name text,
  card_number text,
  set_id uuid,
  set_name text,
  series text,
  rarity text,
  foil_type text,
  foil_type_display text,
  foil_pattern text,
  foil_pattern_display text,
  texture text,
  texture_display text,
  material text,
  material_display text,
  size text,
  stamp_type text,
  source_type text,
  is_secret_rare boolean
) AS $$
BEGIN
  RETURN QUERY
  SELECT
    cv.id AS variant_id,
    cm.id AS card_id,
    cm.name AS card_name,
    cm.card_number,
    cs.id AS set_id,
    cs.name AS set_name,
    cs.series,
    cm.rarity,
    cv.foil_type,
    ft.display_name AS foil_type_display,
    cv.foil_pattern,
    fp.display_name AS foil_pattern_display,
    cv.texture,
    tx.display_name AS texture_display,
    cv.material,
    mt.display_name AS material_display,
    cv.size,
    cv.stamp_type,
    cv.source_type,
    (cs.total_cards IS NOT NULL AND cm.card_number_numeric > cs.total_cards) AS is_secret_rare
  FROM card_variants cv
  JOIN card_master cm ON cm.id = cv.card_id
  JOIN card_sets cs ON cs.id = cm.set_id
  LEFT JOIN foil_types ft ON ft.code = cv.foil_type
  LEFT JOIN foil_patterns fp ON fp.code = cv.foil_pattern
  LEFT JOIN textures tx ON tx.code = cv.texture
  LEFT JOIN materials mt ON mt.code = cv.material
  WHERE (p_series IS NULL OR cs.series = ANY(p_series))
    AND (p_set_ids IS NULL OR cs.id = ANY(p_set_ids))
    AND (p_name_terms IS NULL OR EXISTS (
          SELECT 1 FROM unnest(p_name_terms) t WHERE cm.name ILIKE '%' || t || '%'
        ))
    AND (p_related_pokemon IS NULL OR cm.scenes && p_related_pokemon)
    AND (p_rarities IS NULL OR cm.rarity = ANY(p_rarities))
    AND (p_foil_types IS NULL OR cv.foil_type = ANY(p_foil_types))
    AND (p_foil_patterns IS NULL OR cv.foil_pattern = ANY(p_foil_patterns))
    AND (p_textures IS NULL OR cv.texture = ANY(p_textures))
    AND (p_materials IS NULL OR cv.material = ANY(p_materials))
    AND (p_sizes IS NULL OR cv.size = ANY(p_sizes))
    AND (p_stamp_types IS NULL OR cv.stamp_type = ANY(p_stamp_types))
    AND (p_source_types IS NULL OR cv.source_type = ANY(p_source_types))
    AND (NOT p_exclude_secret_rare OR NOT (cs.total_cards IS NOT NULL AND cm.card_number_numeric > cs.total_cards))
    AND NOT EXISTS (
      SELECT 1 FROM listing_card_assignments lca
      WHERE lca.template_id = p_template_id AND lca.variant_id = cv.id
    )
  ORDER BY cs.name, cm.card_number_numeric
  LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE;

COMMIT;
