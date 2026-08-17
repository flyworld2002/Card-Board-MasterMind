-- Migration 041: card_photos / card_photo_details — per-copy photo
-- library (docs/plans/listing-pricing-system.md build log, 2026-08-17).
-- Revives a design confirmed 2026-08-02 but never built. Corrected this
-- session: scoped to card_variants.id, not card_master.id (a holo and
-- reverse-holo print of the same card look physically different, so
-- need separate photo pools; multiple card_photos rows under one
-- variant_id represent multiple physical copies of that SAME print).
--
-- Additive alongside listing_card_assignments.eps_picture_url (kept,
-- not migrated/dropped) — card_photo_id wins when set, existing rows
-- keep working unchanged via the legacy column until someone
-- deliberately re-stages via the new flow.
--
-- default_card_photo_id() is the single source of truth for "which
-- existing photo group should auto-apply when a card is added to a
-- listing" — called directly from the browser at insert time
-- (openAddCardModal / openBatchAddModal), not just a manual later step.
-- Priority: a group sourced from a listing whose finish_kind matches
-- the target listing's finish_kind, else the most recently created
-- group, else none.

BEGIN;

CREATE TABLE IF NOT EXISTS card_photos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  variant_id uuid NOT NULL REFERENCES card_variants(id) ON DELETE CASCADE,
  front_eps_url text NOT NULL,
  label text,
  has_additional boolean NOT NULL DEFAULT false,
  source_finish_kind text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_card_photos_variant_id ON card_photos(variant_id);

CREATE TABLE IF NOT EXISTS card_photo_details (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  card_photo_id uuid NOT NULL REFERENCES card_photos(id) ON DELETE CASCADE,
  eps_url text NOT NULL,
  label text,
  sort_order integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_card_photo_details_card_photo_id ON card_photo_details(card_photo_id);

ALTER TABLE card_photos ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "authenticated only" ON card_photos;
CREATE POLICY "authenticated only" ON card_photos FOR ALL USING (auth.role() = 'authenticated');

ALTER TABLE card_photo_details ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "authenticated only" ON card_photo_details;
CREATE POLICY "authenticated only" ON card_photo_details FOR ALL USING (auth.role() = 'authenticated');

ALTER TABLE listing_card_assignments
  ADD COLUMN IF NOT EXISTS card_photo_id uuid REFERENCES card_photos(id);

CREATE OR REPLACE FUNCTION default_card_photo_id(p_variant_id uuid, p_target_finish_kind text DEFAULT NULL)
RETURNS uuid AS $$
  SELECT id FROM card_photos
  WHERE variant_id = p_variant_id
  ORDER BY (source_finish_kind IS NOT DISTINCT FROM p_target_finish_kind) DESC,
           created_at DESC
  LIMIT 1;
$$ LANGUAGE sql STABLE;

COMMIT;
