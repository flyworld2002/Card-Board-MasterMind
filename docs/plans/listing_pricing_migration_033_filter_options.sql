-- Migration 033: advanced_search_filter_options() — fixes a real bug in
-- the "+ Batch add cards" filter panel (migration 032 build): rarity and
-- variant-axis dropdown options were built by fetching the WHOLE
-- card_master.rarity / card_variants.<axis> column to the browser and
-- deduping client-side. card_master has 5,823 rows and card_variants has
-- 9,443 — both exceed Supabase/PostgREST's default 1,000-row response
-- cap, so those fetches were silently truncated. Rarities/axis values
-- that only appear later in the table in whatever unordered-by-default
-- sequence Postgres happened to return (e.g. Double Rare/Ultra Rare,
-- common in the newer Mega Evolution-era cards) never made it into the
-- dropdown at all. Fei caught this by noticing Double Rare/Ultra Rare
-- missing from the Rarity filter.
--
-- Fix: compute DISTINCT server-side instead — correct regardless of
-- table size, and a far lighter payload than shipping 5-9k rows to the
-- browser just to dedupe ~15 values.

BEGIN;

DROP FUNCTION IF EXISTS advanced_search_filter_options();

CREATE OR REPLACE FUNCTION advanced_search_filter_options()
RETURNS TABLE (axis text, code text, display_label text) AS $$
  SELECT 'rarity', r.rarity, r.rarity
  FROM (SELECT DISTINCT rarity FROM card_master WHERE rarity IS NOT NULL) r

  UNION ALL
  SELECT 'foil_type', v.foil_type, COALESCE(ft.display_name, v.foil_type)
  FROM (SELECT DISTINCT foil_type FROM card_variants WHERE foil_type IS NOT NULL) v
  LEFT JOIN foil_types ft ON ft.code = v.foil_type

  UNION ALL
  SELECT 'foil_pattern', v.foil_pattern, COALESCE(fp.display_name, v.foil_pattern)
  FROM (SELECT DISTINCT foil_pattern FROM card_variants WHERE foil_pattern IS NOT NULL) v
  LEFT JOIN foil_patterns fp ON fp.code = v.foil_pattern

  UNION ALL
  SELECT 'texture', v.texture, COALESCE(tx.display_name, v.texture)
  FROM (SELECT DISTINCT texture FROM card_variants WHERE texture IS NOT NULL) v
  LEFT JOIN textures tx ON tx.code = v.texture

  UNION ALL
  SELECT 'material', v.material, COALESCE(mt.display_name, v.material)
  FROM (SELECT DISTINCT material FROM card_variants WHERE material IS NOT NULL) v
  LEFT JOIN materials mt ON mt.code = v.material

  UNION ALL
  SELECT 'size', v.size, v.size
  FROM (SELECT DISTINCT size FROM card_variants WHERE size IS NOT NULL) v

  UNION ALL
  SELECT 'stamp_type', v.stamp_type, v.stamp_type
  FROM (SELECT DISTINCT stamp_type FROM card_variants WHERE stamp_type IS NOT NULL) v

  UNION ALL
  SELECT 'source_type', v.source_type, v.source_type
  FROM (SELECT DISTINCT source_type FROM card_variants WHERE source_type IS NOT NULL) v

  ORDER BY 1, 3;
$$ LANGUAGE sql STABLE;

COMMIT;
