-- Migration 020: eBay description navigation system (family strip, era
-- hub-and-spoke, era index). Generated from
-- docs/plans/listing-pricing-system.md, "Plan: eBay description
-- navigation system" (reviewed and locked with Fei).
--
-- All columns nullable/default-off — existing rows and flows unaffected
-- until Fei opts a template in by assigning set_id.
--
-- NOTE on finish-match ambiguity (reviewed with Fei): no uniqueness
-- constraint is added on (set_id, finish_kind). Two templates for the
-- same set/finish is an accepted real scenario (e.g. two reverse-holo
-- listings for one set) — render_description()'s resolution logic
-- handles it by falling back to is_set_primary whenever a finish-match
-- lookup returns anything other than exactly one row (0 or 2+), rather
-- than picking arbitrarily via LIMIT 1. See importer/ebay_descriptions.py.

BEGIN;

ALTER TABLE listing_templates
  ADD COLUMN IF NOT EXISTS set_id uuid REFERENCES card_sets(id),
  ADD COLUMN IF NOT EXISTS finish_kind text,            -- machine key for cross-set finish matching:
                                                         -- 'non_holo' | 'reverse_holo' | 'poke_ball' | 'master_ball' | 'ultra_rare'
                                                         -- NOT the existing listing_kind column (that holds listing
                                                         -- structure, e.g. 'variation' — verified live, leave it alone)
  ADD COLUMN IF NOT EXISTS family_label text,           -- buyer-facing display: 'Non-Holo', 'Reverse Holo', 'Master Ball'
  ADD COLUMN IF NOT EXISTS nav_rank integer,             -- order within the family strip
  ADD COLUMN IF NOT EXISTS is_set_primary boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS show_in_nav boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS nav_image_url text,
  ADD COLUMN IF NOT EXISTS description_live_html text,  -- snapshot of what's live on eBay
  ADD COLUMN IF NOT EXISTS pushed_description_hash text,
  ADD COLUMN IF NOT EXISTS description_pushed_at timestamptz;

-- at most one primary per set
CREATE UNIQUE INDEX IF NOT EXISTS listing_templates_one_primary_per_set
  ON listing_templates (set_id) WHERE is_set_primary;

COMMIT;
