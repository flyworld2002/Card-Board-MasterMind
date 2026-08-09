-- Migration 027: family_description — a per-listing display description
-- distinct from family_label. family_label is the short tile heading
-- ("Non-Holo", "Reverse Holo"); family_description is a separate,
-- listing-driven blurb exposed as {{item_description}} inside custom
-- family_tile item templates. Nullable/additive — the built-in Python
-- tile rendering doesn't use it (no design slot for it there); it's only
-- available to hand-authored item templates.

BEGIN;

ALTER TABLE listing_templates
  ADD COLUMN IF NOT EXISTS family_description text;

COMMIT;
