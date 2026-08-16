-- Migration 037: render_variation_name() — Postgres port of
-- importer/ebay_listing_sync.py's _render_variation_name(), so the
-- browser can compute the real buyer-facing eBay variation name (not a
-- generic placeholder) for the roster table's custom_name preview + Fill
-- button (docs/plans/listing-pricing-system.md build log, 2026-08-16).
--
-- Faithful port: same tokens ({number}, {number:pad}, {set_total},
-- {prefix}, {name}, {suffix}), same suffix build order (foil_pattern,
-- then "Reverse Holo RH" for foil_type='reverse_holo', then stamp_type),
-- same promo-set-prefix-no-total_cards default-format fallback, same
-- final whitespace-collapse. _humanize()'s Python `.replace('_',' ')
-- .title()` becomes `initcap(replace(x, '_', ' '))` here — equivalent
-- for these specific lowercase/underscore code values (foil_pattern/
-- stamp_type codes never contain apostrophes or mixed case).
--
-- Verified byte-identical against the existing Python function across
-- every real roster variant before this was relied on for anything —
-- see build log. Whether importer/ebay_listing_sync.py's Python function
-- was refactored to call this RPC (vs. kept as an independent parallel
-- implementation) is also noted there.

BEGIN;

CREATE OR REPLACE FUNCTION render_variation_name(p_variant_id uuid, p_template_id uuid DEFAULT NULL)
RETURNS text AS $$
DECLARE
  v_card_name text;
  v_card_number text;
  v_total_cards integer;
  v_set_prefix text;
  v_foil_type text;
  v_foil_pattern text;
  v_stamp_type text;
  v_name_format text;
  v_numbered_default constant text := '{number}/{set_total} {name} {suffix}';
  v_suffix_parts text[] := '{}';
  v_suffix text;
  v_padded text;
  v_rendered text;
BEGIN
  SELECT cm.name, cm.card_number, cs.total_cards, cs.set_prefix,
         cv.foil_type, cv.foil_pattern, cv.stamp_type
    INTO v_card_name, v_card_number, v_total_cards, v_set_prefix,
         v_foil_type, v_foil_pattern, v_stamp_type
  FROM card_variants cv
  JOIN card_master cm ON cv.card_id = cm.id
  JOIN card_sets cs ON cm.set_id = cs.id
  WHERE cv.id = p_variant_id;

  IF NOT FOUND THEN
    RETURN NULL;
  END IF;

  IF p_template_id IS NOT NULL THEN
    SELECT name_format INTO v_name_format FROM listing_templates WHERE id = p_template_id;
  END IF;

  IF v_name_format IS NULL OR v_name_format = v_numbered_default THEN
    IF v_set_prefix IS NOT NULL AND v_total_cards IS NULL THEN
      v_name_format := '{prefix} {number} {name} {suffix}';
    ELSE
      v_name_format := v_numbered_default;
    END IF;
  END IF;

  IF v_foil_pattern IS NOT NULL THEN
    v_suffix_parts := array_append(v_suffix_parts, initcap(replace(v_foil_pattern, '_', ' ')));
  END IF;
  IF v_foil_type = 'reverse_holo' THEN
    v_suffix_parts := array_append(v_suffix_parts, 'Reverse Holo RH');
  END IF;
  IF v_stamp_type IS NOT NULL THEN
    v_suffix_parts := array_append(v_suffix_parts, initcap(replace(v_stamp_type, '_', ' ')));
  END IF;
  v_suffix := array_to_string(v_suffix_parts, ' ');

  -- lpad() TRUNCATES a string already >= the target length (unlike
  -- Python's zfill(), which never truncates) — guard with the length
  -- check so a secret-rare card_number longer than the set's digit
  -- count (a real, already-handled scenario — see is_secret_rare in
  -- search_roster_candidates(), migration 032) never gets silently
  -- clipped.
  v_padded := v_card_number;
  IF v_total_cards IS NOT NULL AND v_card_number ~ '^[0-9]+$'
     AND length(v_card_number) < length(v_total_cards::text) THEN
    v_padded := lpad(v_card_number, length(v_total_cards::text), '0');
  END IF;

  v_rendered := v_name_format;
  v_rendered := replace(v_rendered, '{number:pad}', COALESCE(v_padded, ''));
  v_rendered := replace(v_rendered, '{number}', COALESCE(v_card_number, ''));
  v_rendered := replace(v_rendered, '{set_total}', COALESCE(v_total_cards::text, ''));
  v_rendered := replace(v_rendered, '{prefix}', COALESCE(v_set_prefix, ''));
  v_rendered := replace(v_rendered, '{name}', v_card_name);
  v_rendered := replace(v_rendered, '{suffix}', v_suffix);

  RETURN trim(regexp_replace(v_rendered, '\s+', ' ', 'g'));
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION render_variation_names(p_template_id uuid, p_variant_ids uuid[])
RETURNS TABLE(variant_id uuid, rendered_name text) AS $$
  SELECT vid, render_variation_name(vid, p_template_id) FROM unnest(p_variant_ids) AS vid;
$$ LANGUAGE sql STABLE;

COMMIT;
