-- Migration 017: listing_templates was missing item_postal_code.
-- Confirmed live via a real GetItem call (336204674240): eBay's item
-- location is three separate fields (Country, Location, PostalCode),
-- not two — PostalCode is the actual zip AddFixedPriceItem needs for
-- shipping calculation, Location is just a free-text display string.
-- Migration 016 only added item_location/item_country.

ALTER TABLE listing_templates
  ADD COLUMN IF NOT EXISTS item_postal_code text;
