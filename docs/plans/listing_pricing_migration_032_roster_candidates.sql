-- Migration 032: search_roster_candidates() — the generic advanced-search
-- RPC behind the web app's "+ Batch add cards" feature (Fei, 8/16:
-- creating a "Mega Evolution ex" listing template, wants to populate its
-- roster without hand-picking 65+ cards one at a time via the existing
-- "Add card to listing" search-one-click-one flow, and wants the filter
-- mechanism to be generic/reusable across future themed listings — era,
-- set, Pokémon name (+ evolution line, resolved client-side against the
-- pokemon/pokemon_evolutions species mirror and folded into
-- p_name_terms), rarity, all seven card_variants axes, and a "related
-- Pokémon" hook for card_master.scenes).
--
-- Single source of truth for this filter logic, same convention as
-- resolve_listing_prices() (migration 004) — any future caller (a
-- standalone search tool, a CLI) gets identical results, never a
-- re-implementation that can drift.
--
-- Every array filter is NULL-means-unconstrained: pass NULL (the
-- default) for any axis you're not filtering on, not an empty array.
-- p_related_pokemon will match zero rows until card_master.scenes is
-- actually populated (currently empty everywhere, no importer writes it
-- yet) — the filter ships now so nothing else has to change once Fei
-- backfills it himself.
--
-- Always excludes variant rows already on p_template_id's roster in any
-- status (active/queued/sold_out_retained) — the same dedup
-- importExisting() does client-side for the platform_listings-import
-- path (card-board-mastermind-WebInvManagement/listing-pricing.js),
-- moved server-side here so every caller gets it for free.

BEGIN;

DROP FUNCTION IF EXISTS search_roster_candidates(
  uuid, text[], uuid[], text[], text[], text[], text[], text[], text[], text[], text[], text[], text[], boolean
);

CREATE OR REPLACE FUNCTION search_roster_candidates(
  p_template_id uuid,
  p_series text[] DEFAULT NULL,          -- card_sets.series (era)
  p_set_ids uuid[] DEFAULT NULL,
  p_name_terms text[] DEFAULT NULL,      -- ILIKE '%term%', OR'd across terms
  p_related_pokemon text[] DEFAULT NULL, -- cm.scenes && this
  p_rarities text[] DEFAULT NULL,
  p_foil_types text[] DEFAULT NULL,
  p_foil_patterns text[] DEFAULT NULL,
  p_textures text[] DEFAULT NULL,
  p_materials text[] DEFAULT NULL,
  p_sizes text[] DEFAULT NULL,
  p_stamp_types text[] DEFAULT NULL,
  p_source_types text[] DEFAULT NULL,
  p_exclude_secret_rare boolean DEFAULT true
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
  ORDER BY cs.name, cm.card_number_numeric;
END;
$$ LANGUAGE plpgsql STABLE;

COMMIT;
