-- Migration 024: finish-kind fallback labels join description_theme_settings.
-- Previously the FINISH_LABELS constant in importer/ebay_descriptions.py —
-- used only when a template's own family_label isn't set. Per-listing
-- family_label (Edit-fields modal) still always takes priority; these are
-- just the global defaults shown when it's blank.

BEGIN;

INSERT INTO description_theme_settings (key, value, label, category) VALUES
  ('finish_label_non_holo',     'Non-Holo',      'Finish label: non_holo',      'text'),
  ('finish_label_reverse_holo', 'Reverse Holo',  'Finish label: reverse_holo',  'text'),
  ('finish_label_poke_ball',    'Poké Ball',     'Finish label: poke_ball',     'text'),
  ('finish_label_master_ball',  'Master Ball',   'Finish label: master_ball',   'text'),
  ('finish_label_ultra_rare',   'Ultra Rare',    'Finish label: ultra_rare',    'text')
ON CONFLICT (key) DO NOTHING;

COMMIT;
