-- Migration 028: per-theme scoping for description_theme_settings + a
-- theme_key column on listing_templates, so different shops/listing
-- groups can each carry their own colors/sizing/text instead of one
-- theme applying to every render (Fei, 8/09: "For biggyfish, I can have
-- one theme. For other user I can have another, Maybe within the same
-- user, I can have another theme.").
--
-- All existing rows become the 'default' theme. Any listing_templates
-- row with theme_key IS NULL renders with 'default'
-- (importer/ebay_descriptions.py's _load_theme()), so nothing regresses
-- until Fei deliberately assigns a template to a different theme.

BEGIN;

ALTER TABLE description_theme_settings
  ADD COLUMN IF NOT EXISTS theme_key text NOT NULL DEFAULT 'default';

ALTER TABLE description_theme_settings DROP CONSTRAINT IF EXISTS description_theme_settings_pkey;
ALTER TABLE description_theme_settings
  ADD CONSTRAINT description_theme_settings_pkey PRIMARY KEY (theme_key, key);

ALTER TABLE listing_templates
  ADD COLUMN IF NOT EXISTS theme_key text;

COMMIT;
