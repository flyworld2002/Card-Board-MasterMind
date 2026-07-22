-- Migration 003: template-centric roster + manual groups
-- Generated from docs/plans/listing-pricing-system.md's PIVOT section.
-- listing_pricing_rules had 0 rows and is dropped cleanly (retired,
-- replaced by manual group->profile assignment).

BEGIN;

ALTER TABLE listing_templates ADD COLUMN IF NOT EXISTS listing_id text;
CREATE UNIQUE INDEX IF NOT EXISTS idx_listing_templates_platform_listing_id
  ON listing_templates(platform, listing_id) WHERE listing_id IS NOT NULL;
  -- Nullable: a "draft" template not yet tied to a real eBay listing is
  -- still allowed (e.g. planning a roster before the listing exists).

CREATE TABLE IF NOT EXISTS listing_card_groups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  template_id uuid NOT NULL REFERENCES listing_templates(id) ON DELETE CASCADE,
  name text NOT NULL,
  profile_id uuid REFERENCES pricing_profiles(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (template_id, name)
);

ALTER TABLE listing_card_assignments ADD COLUMN IF NOT EXISTS template_id uuid REFERENCES listing_templates(id);
ALTER TABLE listing_card_assignments ADD COLUMN IF NOT EXISTS group_id uuid REFERENCES listing_card_groups(id);

-- Caught during testing: the old chk_lca_has_listing_ref constraint (from
-- session 1, requiring platform_listing_id OR ebay_item_id) rejects a
-- queued row that only has template_id set — but template_id is now THE
-- canonical listing reference (via listing_templates.listing_id), making
-- ebay_item_id redundant. Table was still empty, safe to tighten.
ALTER TABLE listing_card_assignments DROP CONSTRAINT IF EXISTS chk_lca_has_listing_ref;
ALTER TABLE listing_card_assignments ALTER COLUMN template_id SET NOT NULL;
ALTER TABLE listing_card_assignments DROP COLUMN IF EXISTS ebay_item_id;

DROP TABLE IF EXISTS listing_pricing_rules;

-- RLS on the 2 new/changed tables, matching the project convention
-- (see finding in docs/plans/listing-pricing-system.md's build log).
ALTER TABLE listing_card_groups ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated only" ON listing_card_groups
  FOR ALL TO public USING (auth.role() = 'authenticated');

COMMIT;
