-- Migration 023: description nav theme settings — small, quick-editable
-- knobs (colors, sizing, button/label text) for the family_nav/era_nav/
-- era_index token renderer in importer/ebay_descriptions.py. Previously
-- hardcoded (DEEP_SEA dict + inline string literals); Fei asked for a
-- way to make small tweaks (esp. text) without needing a code change
-- each time.
--
-- Fixed, code-referenced key set (not a freeform library like
-- description_sections) — the renderer falls back to DEFAULT_THEME for
-- any key missing here, so a deleted/blank row never breaks rendering.

BEGIN;

CREATE TABLE IF NOT EXISTS description_theme_settings (
  key text PRIMARY KEY,
  value text NOT NULL,
  label text NOT NULL,
  category text NOT NULL DEFAULT 'text',  -- 'color' | 'size' | 'text'
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO description_theme_settings (key, value, label, category) VALUES
  ('color_bg',         '#0a1f38', 'Background (main)',        'color'),
  ('color_panel',      '#12365c', 'Panel background',         'color'),
  ('color_border',     '#1d4d7a', 'Border',                   'color'),
  ('color_cyan',       '#3fc3e8', 'Accent',                   'color'),
  ('color_text_light', '#eaf6fb', 'Text (light)',             'color'),
  ('color_text_muted', '#7fa8c9', 'Text (muted)',             'color'),
  ('color_text_dim',   '#9db2c6', 'Text (dim)',                'color'),
  ('nav_tile_width',   '140',     'Family tile image width (px)', 'size'),
  ('text_view_listing',      'View listing',              'Family tile button (not current)', 'text'),
  ('text_viewing_this',      'Viewing this',              'Family tile button (current)',      'text'),
  ('text_family_nav_title',  'Shop this set',             'Family strip heading',              'text'),
  ('text_era_list_link',     'Shop this set',             'Era row link text',                 'text'),
  ('text_era_nav_subtitle',  'Same finish, other sets:',  'Era grid subheading',                'text'),
  ('text_youre_here_suffix', '(you''re here)',            'Era chip "current" suffix',          'text')
ON CONFLICT (key) DO NOTHING;

COMMIT;
