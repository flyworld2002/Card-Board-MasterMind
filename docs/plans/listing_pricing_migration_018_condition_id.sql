-- Migration 018: listing_templates needs condition_id too — confirmed
-- live (336204674240 uses ConditionID=4000, "Ungraded", the trading-card
-- -category condition code) — AddFixedPriceItem requires it and it's
-- exactly the kind of listing-level metadata migration 016 already
-- established the pattern for (clone or manual, both write the same
-- column).

ALTER TABLE listing_templates
  ADD COLUMN IF NOT EXISTS condition_id text;
