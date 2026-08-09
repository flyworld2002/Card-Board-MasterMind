-- Migration 021: reusable description section/layout library.
-- Generated from docs/plans/listing-pricing-system.md, "Reusable section
-- library + layouts (DB-backed — Fei, 8/08)". Supersedes the hardcoded
-- DESCRIPTION_PRESETS constant in importer/ebay_descriptions.py —
-- /api/description-presets now reads this table instead.
--
-- kind='layout' rows are whole-description starters (feed the existing
-- Insert-layout dropdown, REPLACE the textarea). kind='section' rows are
-- small reusable blocks (feed the new Insert-section dropdown, insert AT
-- CURSOR). Both may contain {{tokens}} — substituted by the same
-- render_description() pass either way.

BEGIN;

CREATE TABLE IF NOT EXISTS description_sections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key text UNIQUE NOT NULL,
  label text NOT NULL,
  html text NOT NULL,
  kind text NOT NULL DEFAULT 'section',   -- 'section' | 'layout'
  sort_order integer NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Seed: the existing Spoke/Hub layouts (theme-neutral — NOT the Deep Sea
-- mockup, which is a separate, still-undecided styling question). Content
-- copied verbatim from the DESCRIPTION_PRESETS constant this table
-- replaces, so the Insert-layout dropdown doesn't regress.
INSERT INTO description_sections (key, label, html, kind, sort_order)
VALUES
  ('spoke', 'Spoke (regular listing in an era)',
$$<h1>{{set_name}}</h1>
<p>All cards are shipped in a penny sleeve and a rigid card saver, in a
bubble mailer. Condition is graded to the best of our ability — please see
photos and reach out with any questions before purchasing.</p>
{{family_nav}}
{{era_hub_link}}
{{era_index}}
$$,
   'layout', 1),
  ('hub', 'Hub (era base-set listing)',
$$<h1>{{set_name}} — {{series_name}} era</h1>
<p>All cards are shipped in a penny sleeve and a rigid card saver, in a
bubble mailer. Condition is graded to the best of our ability — please see
photos and reach out with any questions before purchasing.</p>
{{family_nav}}
{{era_nav}}
{{era_index}}
$$,
   'layout', 2)
ON CONFLICT (key) DO NOTHING;

COMMIT;
