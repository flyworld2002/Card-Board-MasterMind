-- Migration 029: description_sections -> module builder (kind=static/
-- repeater/single, rule-driven rendering, byte-identical to today's
-- output). Fei, 8/09: "I want to design a full customizable description
-- module builder. Current design just doesn't look right to me." —
-- replaces the reserved-key/{{token:modifier}} special cases built up
-- earlier this session with one coherent per-module model.
--
-- Verified against live data: none of the 5 reserved item-template keys
-- (family_tile/family_tile_current/era_row/era_chip/era_chip_current)
-- exist as real rows yet, so the override path has never actually fired
-- in production — this migration is safe by construction, every live
-- listing keeps rendering through the Python defaults it already uses.
-- backfill_description_modules() (importer/ebay_descriptions.py) does
-- the actual data seeding/conversion; this migration is schema-only.
--
-- html is relaxed to nullable — a 'repeater'/'single' module renders via
-- item_template_html instead, html goes unused for those kinds (kept
-- NULL rather than filled with a meaningless placeholder). Application-
-- level validation (create/update_description_section) enforces html
-- being required for kind='static' instead, since that's a business
-- rule, not a structural one every future kind will share.

BEGIN;

ALTER TABLE description_sections
  ALTER COLUMN html DROP NOT NULL,
  ADD COLUMN IF NOT EXISTS repeat_rule text,
  ADD COLUMN IF NOT EXISTS layout text,
  ADD COLUMN IF NOT EXISTS item_template_html text,
  ADD COLUMN IF NOT EXISTS item_template_current_html text,
  ADD COLUMN IF NOT EXISTS title text,
  ADD COLUMN IF NOT EXISTS subtitle text;

-- Rename only, no data change — 'layout' replaced-the-whole-textarea vs
-- 'section' inserted-at-cursor was purely an authoring-time UX
-- distinction, not a rendering one; both are static HTML either way.
UPDATE description_sections SET kind = 'static' WHERE kind IN ('section', 'layout');

-- 'item_template' stays a legal value (existing rows are converted by
-- backfill_description_modules(), run right after this migration, not
-- by this migration itself) — kept in the CHECK permanently rather than
-- tightened in a follow-up migration, since the UI simply stops
-- producing it going forward.
ALTER TABLE description_sections DROP CONSTRAINT IF EXISTS description_sections_kind_check;
ALTER TABLE description_sections
  ADD CONSTRAINT description_sections_kind_check
  CHECK (kind IN ('static', 'repeater', 'single', 'item_template'));

COMMIT;
