-- Migration 046: {set_total} uses base_set_number's stored text directly,
-- no computed padding
--
-- Migration 044 cast base_set_number to integer and then re-derived
-- padding from number_pad_width -- unnecessary round-trip. Confirmed
-- (2026-08-18) base_set_number is already stored correctly formatted
-- as text (e.g. '086', '025', '073') -- whoever set it already padded
-- it right. Trust that stored text directly instead of stripping and
-- recomputing it. Falls back to total_cards::text (no padding applied,
-- shown as-is) only when base_set_number is unset.

BEGIN;

CREATE OR REPLACE FUNCTION render_variation_name(p_variant_id uuid, p_template_id uuid DEFAULT NULL) RETURNS text AS $$
DECLARE
  v_card_name text;
  v_card_number text;
  v_total_cards integer;
  v_base_set_number text;
  v_set_prefix text;
  v_number_pad_width integer;
  v_foil_type text;
  v_foil_pattern text;
  v_stamp_type text;
  v_name_format text;
  v_numbered_default constant text := '{number}/{set_total} {name} {suffix}';
  v_suffix_parts text[] := '{}';
  v_suffix text;
  v_padded text;
  v_set_total_text text;
  v_rendered text;
BEGIN
  SELECT cm.name, cm.card_number, cs.total_cards, cs.base_set_number, cs.set_prefix, cs.number_pad_width,
         cv.foil_type, cv.foil_pattern, cv.stamp_type
    INTO v_card_name, v_card_number, v_total_cards, v_base_set_number, v_set_prefix, v_number_pad_width,
         v_foil_type, v_foil_pattern, v_stamp_type
  FROM card_variants cv
  JOIN card_master cm ON cv.card_id = cm.id
  JOIN card_sets cs ON cm.set_id = cs.id
  WHERE cv.id = p_variant_id;

  IF NOT FOUND THEN
    RETURN NULL;
  END IF;

  -- base_set_number is already stored correctly formatted (e.g. '086') --
  -- use it as-is; only fall back to total_cards (unpadded) when unset.
  v_set_total_text := COALESCE(
    CASE WHEN v_base_set_number ~ '^[0-9]+$' THEN v_base_set_number END,
    v_total_cards::text
  );

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

  v_padded := v_card_number;
  IF v_number_pad_width IS NOT NULL AND v_number_pad_width > 0
     AND v_card_number ~ '^[0-9]+$'
     AND length(v_card_number) < v_number_pad_width THEN
    v_padded := lpad(v_card_number, v_number_pad_width, '0');
  END IF;

  v_rendered := v_name_format;
  v_rendered := replace(v_rendered, '{number:pad}', COALESCE(v_padded, ''));
  v_rendered := replace(v_rendered, '{number}', COALESCE(v_padded, ''));
  v_rendered := replace(v_rendered, '{set_total}', COALESCE(v_set_total_text, ''));
  v_rendered := replace(v_rendered, '{prefix}', COALESCE(v_set_prefix, ''));
  v_rendered := replace(v_rendered, '{name}', v_card_name);
  v_rendered := replace(v_rendered, '{suffix}', v_suffix);

  RETURN trim(regexp_replace(v_rendered, '\s+', ' ', 'g'));
END;
$$ LANGUAGE plpgsql STABLE;

COMMIT;
