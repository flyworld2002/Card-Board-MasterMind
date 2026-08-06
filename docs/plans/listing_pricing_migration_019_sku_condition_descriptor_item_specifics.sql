-- Migration 019: three more fields confirmed via real GetItem data and
-- eBay's official docs, none guessed:
--
-- sku (text): confirmed on a real listing (336691613250, 'VarSinglesHolo')
-- as a genuine top-level <Item><SKU> — a single free-text "custom label"
-- for the WHOLE listing, not per-variation (every real <Variation> on
-- that listing has no SKU of its own).
--
-- condition_descriptor_value (text): what eBay's Sell form calls "Card
-- Condition" (Near Mint/Lightly Played/etc.) is a SEPARATE field from
-- condition_id, transmitted as ConditionDescriptors/ConditionDescriptor
-- with Name=40001 (fixed, confirmed live) and a numeric Value. Per
-- eBay's docs for category 183454 (Fei's real category): 400010=Near
-- mint or better, 400015=Lightly played (Excellent),
-- 400016=Moderately played (Very good), 400017=Heavily played (Poor).
-- The descriptor Name (40001) is a fixed constant handled in code, not
-- stored — only the Value varies.
--
-- item_specifics (jsonb): eBay's ItemSpecifics is an arbitrary
-- Name->Value NameValueList, confirmed live with 8 real pairs (Game,
-- Set, Language, Manufacturer, Year Manufactured, Card Type,
-- Country/Region of Manufacture, Country of Origin) plus Fei's ask for
-- a "Character" field. JSONB instead of one column per specific name —
-- this set isn't fixed/closed (categories vary, Fei may add more later)
-- and a flexible bag matches eBay's own NameValueList shape directly.

ALTER TABLE listing_templates
  ADD COLUMN IF NOT EXISTS sku text,
  ADD COLUMN IF NOT EXISTS condition_descriptor_value text,
  ADD COLUMN IF NOT EXISTS item_specifics jsonb NOT NULL DEFAULT '{}'::jsonb;
