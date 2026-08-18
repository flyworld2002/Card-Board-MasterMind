-- Migration 049: assign_card_photo() RPC — browser-callable, no
-- picking_api.py round-trip needed.
--
-- Fei's call (2026-08-18): browsing/selecting an EXISTING photo group
-- for a card is a plain DB read+write with no eBay dependency at all,
-- but the web UI was routing it through picking_api.py's
-- /api/assign-card-photo endpoint anyway (same as the EPS-upload
-- endpoints, which DO need that service for real eBay calls) — so the
-- whole "Manage photos" modal stalled whenever the desktop's
-- CBMPickingAPI scheduled task was down, even though nothing about
-- picking an already-uploaded photo actually needed it.
--
-- This RPC is the Postgres-side twin of importer/card_photos.py's
-- assign_card_photo() (same validation, same error text where
-- reasonable) — that Python function stays as-is (still used by the
-- "create new photo group" flow's follow-up assign, which is already
-- mid-flight through picking_api.py for the actual EPS upload, so
-- there's no benefit routing it through this RPC instead). Only the
-- "click an existing photo to select it" path in listing-pricing.js
-- switches to calling this directly.

BEGIN;

CREATE OR REPLACE FUNCTION assign_card_photo(p_row_id uuid, p_card_photo_id uuid) RETURNS void AS $$
DECLARE
  v_status text;
  v_row_variant_id uuid;
  v_photo_variant_id uuid;
BEGIN
  SELECT status, variant_id INTO v_status, v_row_variant_id
  FROM listing_card_assignments WHERE id = p_row_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'no such roster row';
  END IF;

  IF v_status NOT IN ('queued', 'active') THEN
    RAISE EXCEPTION 'row is % — pictures can only be assigned for queued or active cards', v_status;
  END IF;

  SELECT variant_id INTO v_photo_variant_id FROM card_photos WHERE id = p_card_photo_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no such photo group';
  END IF;

  IF v_photo_variant_id IS DISTINCT FROM v_row_variant_id THEN
    RAISE EXCEPTION 'that photo group belongs to a different card variant';
  END IF;

  UPDATE listing_card_assignments SET card_photo_id = p_card_photo_id, updated_at = now() WHERE id = p_row_id;
END;
$$ LANGUAGE plpgsql;

COMMIT;
