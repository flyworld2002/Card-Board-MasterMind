-- Migration 031: nav_tile_height — decouples family-tile image height from
-- width (previously locked to the 76:106 card-art aspect ratio). Fei
-- (8/11): tile photos aren't always card-shaped (set box art, banners),
-- so height needed to be its own adjustable knob, same as nav_tile_width.
--
-- Seeded at 195 for EVERY existing theme_key — the value width=140 already
-- computed under the old ratio formula, so nothing renders differently
-- until a theme's height is deliberately changed.

BEGIN;

INSERT INTO description_theme_settings (theme_key, key, value, label, category)
SELECT DISTINCT theme_key, 'nav_tile_height', '195', 'Family tile image height (px)', 'size'
FROM description_theme_settings
ON CONFLICT (theme_key, key) DO NOTHING;

COMMIT;
