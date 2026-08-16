-- Migration 035: rarities lookup table — real display_sort support
-- (docs/plans/listing-pricing-system.md build log, 2026-08-16). No
-- rarity-ordering table exists anywhere in the schema before this;
-- foil_types/foil_patterns/textures/materials all have code/display_name/
-- sort_order, rarities didn't. Same shape, for the same reason: sortable,
-- editable without a code change.
--
-- Seeded with the 15 distinct card_master.rarity values confirmed live
-- this session. Tier order is a best-effort default (standard Pokémon
-- TCG collector convention) — NOT confirmed with Fei value-by-value.
-- Trivially correctable: `UPDATE rarities SET sort_order = ... WHERE
-- code = '...'`, no migration needed to fix ordering later. A rarity
-- with no matching row here (a future new rarity name) sorts last via
-- COALESCE(..., 999999) in resolve_listing_prices(), not first or error.

BEGIN;

CREATE TABLE IF NOT EXISTS rarities (
  code         text PRIMARY KEY,
  display_name text NOT NULL,
  sort_order   integer NOT NULL
);

ALTER TABLE rarities ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "authenticated only" ON rarities;
CREATE POLICY "authenticated only" ON rarities FOR ALL USING (auth.role() = 'authenticated');

INSERT INTO rarities (code, display_name, sort_order) VALUES
  ('Common', 'Common', 10),
  ('Uncommon', 'Uncommon', 20),
  ('Rare', 'Rare', 30),
  ('Rare Holo', 'Rare Holo', 40),
  ('Double Rare', 'Double Rare', 50),
  ('ACE SPEC Rare', 'ACE SPEC Rare', 60),
  ('Ultra Rare', 'Ultra Rare', 70),
  ('Illustration Rare', 'Illustration Rare', 80),
  ('Special Illustration Rare', 'Special Illustration Rare', 90),
  ('Shiny Rare', 'Shiny Rare', 100),
  ('Shiny Ultra Rare', 'Shiny Ultra Rare', 110),
  ('Hyper Rare', 'Hyper Rare', 120),
  ('Mega Hyper Rare', 'Mega Hyper Rare', 130),
  ('MEGA_ATTACK_RARE', 'Mega Attack Rare', 140),
  ('Promo', 'Promo', 150)
ON CONFLICT (code) DO NOTHING;

COMMIT;
