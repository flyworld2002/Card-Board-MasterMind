-- Migration 030: description_sections kind='layout' — a complete,
-- ready-to-use description composed from other modules (Fei, 8/10:
-- "I don't want all these module and block build within Edit listing
-- Meta page... all I need in Edit listing meta page is just pick a
-- layout and done!").
--
-- Renders IDENTICALLY to kind='static' (literal HTML, recursive
-- {{token}} substitution — a layout's html is typically just a wrapper
-- shell referencing header/set_band/family_nav/era_nav/etc. by key) —
-- the distinction is purely semantic/organizational: 'static' rows are
-- small reusable pieces (header, footer, ...), 'layout' rows are the
-- complete thing you pick on a listing's Edit-fields page. No new
-- render-engine columns needed; only the CHECK constraint changes.

BEGIN;

ALTER TABLE description_sections DROP CONSTRAINT IF EXISTS description_sections_kind_check;
ALTER TABLE description_sections
  ADD CONSTRAINT description_sections_kind_check
  CHECK (kind IN ('static', 'repeater', 'single', 'item_template', 'layout'));

COMMIT;
