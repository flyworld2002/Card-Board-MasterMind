-- Migration 039: {set_total} now resolves to card_sets.base_set_number
-- (the actual printed denominator on the card), falling back to
-- total_cards only when base_set_number isn't set — was always
-- total_cards before, which is the FULL catalog count including secret/
-- hyper rares, not what real cards print (e.g. "Ascended Heroes" prints
-- "X/217", not "X/295"). Caught by Fei after fixing {number:pad}.
--
-- Confirmed explicitly with Fei before applying: this is a "fix it
-- globally now" call, not opt-in. 64 already-live listings use
-- '{number}/{set_total} {name} {suffix}' as their name_format —
-- render_variation_name() is only ever invoked at the moment a card is
-- PROMOTED (queued -> active), never re-run against an already-active
-- card (confirmed this session — push_prices()/_compute_changes looks
-- up already-active variations by their stored, frozen external_id,
-- never recomputes/renames them). So nothing already live renames
-- itself. But any NEW card promoted into one of those 64 listings from
-- now on will use the corrected (smaller, base_set_number) denominator
-- while sibling variations already on that listing keep whatever
-- denominator was live when THEY were promoted — a real, visible
-- mismatch within a single listing until/unless those are separately
-- reconciled. Accepted trade-off, not an oversight.

BEGIN;

CREATE OR REPLACE FUNCTION render_variation_name(p_variant_id uuid, p_template_id uuid DEFAULT NULL)
RETURNS text AS $$
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
  v_set_total integer;
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

  -- base_set_number is stored zero-padded (e.g. '217') — cast to
  -- integer to get the real printed denominator, not a zero-padded
  -- string. Falls back to total_cards when base_set_number is unset
  -- (still true for a handful of sets, e.g. promo/energy grab-bags).
  v_set_total := COALESCE(
    CASE WHEN v_base_set_number ~ '^[0-9]+$' THEN v_base_set_number::integer END,
    v_total_cards
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
  v_rendered := replace(v_rendered, '{number}', COALESCE(v_card_number, ''));
  v_rendered := replace(v_rendered, '{set_total}', COALESCE(v_set_total::text, ''));
  v_rendered := replace(v_rendered, '{prefix}', COALESCE(v_set_prefix, ''));
  v_rendered := replace(v_rendered, '{name}', v_card_name);
  v_rendered := replace(v_rendered, '{suffix}', v_suffix);

  RETURN trim(regexp_replace(v_rendered, '\s+', ' ', 'g'));
END;
$$ LANGUAGE plpgsql STABLE;

COMMIT;
