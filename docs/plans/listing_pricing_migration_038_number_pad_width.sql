-- Migration 038: card_sets.number_pad_width — explicit per-set control
-- over {number:pad} zero-padding (docs/plans/listing-pricing-system.md
-- build log, 2026-08-16). Real Pokémon TCG sets use inconsistent card
-- number padding conventions (some 2-digit, some 3-digit, some none at
-- all) — a single derived rule can't get every set right, so this
-- replaces render_variation_name()'s (migration 037) total_cards-derived
-- guess with an explicit setting, editable per set from Configuration.
--
-- Left NULL for every set (no backfill here) — NULL/0 means {number:pad}
-- behaves exactly like plain {number} (no padding), an intentional
-- "not configured" state rather than a guessed default written without
-- Fei seeing it first. Safe to change with zero live impact: no real
-- listing_templates.name_format currently uses {number:pad} (confirmed
-- live this session).

BEGIN;

ALTER TABLE card_sets ADD COLUMN IF NOT EXISTS number_pad_width integer;

CREATE OR REPLACE FUNCTION render_variation_name(p_variant_id uuid, p_template_id uuid DEFAULT NULL)
RETURNS text AS $$
DECLARE
  v_card_name text;
  v_card_number text;
  v_total_cards integer;
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
  v_rendered text;
BEGIN
  SELECT cm.name, cm.card_number, cs.total_cards, cs.set_prefix, cs.number_pad_width,
         cv.foil_type, cv.foil_pattern, cv.stamp_type
    INTO v_card_name, v_card_number, v_total_cards, v_set_prefix, v_number_pad_width,
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

  -- Explicit per-set width now (Configuration -> Sets -> Number padding),
  -- not derived from total_cards. lpad() TRUNCATES a string already >=
  -- the target length, so the length check still guards against ever
  -- clipping a card_number that's already at or past the configured
  -- width (e.g. a secret rare numbered beyond a small pad width).
  v_padded := v_card_number;
  IF v_number_pad_width IS NOT NULL AND v_number_pad_width > 0
     AND v_card_number ~ '^[0-9]+$'
     AND length(v_card_number) < v_number_pad_width THEN
    v_padded := lpad(v_card_number, v_number_pad_width, '0');
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

COMMIT;
