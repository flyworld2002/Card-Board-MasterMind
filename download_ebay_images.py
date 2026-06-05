"""
importer/ebay_image_downloader.py
Download eBay listing images with professional naming convention.

Filename format:
    {set_id}_{card_number}_{Card_Name}_{Variant}.jpg
    {set_id}_{card_number}_{Card_Name}_{Variant}_{Source}.jpg  ← if source present

Examples:
    sv8_013_Roller_Reverse_Holo.jpg
    sv4_049_Golisopod_Normal_Deck_Exclusive.jpg
    svp_044_Charmander_Normal_Promo.jpg
    sv8_091_Palossand_ex_Normal.jpg

Sidecar JSON (same name, .json extension):
    sv8_013_Roller_Reverse_Holo.json
    {
        "card_id":      "full-uuid",
        "card_name":    "Roller",
        "card_number":  "13",
        "set_code":     "sv8",
        "set_name":     "Surging Sparks",
        "variant_type": "Reverse Holo",
        "source_type":  null,
        "ebay_item_id": "335662210469",
        "image_path":   "images/ebay/sv8/sv8_013_Roller_Reverse_Holo.jpg",
        "downloaded_at": "2026-06-05"
    }

DB storage:
    card_master.image_url_own = "images/ebay/sv8/sv8_013_Roller_Reverse_Holo.jpg"

Storage structure:
    images/ebay/
        sv8/                          ← organized by set code
            sv8_013_Roller_Reverse_Holo.jpg
            sv8_013_Roller_Reverse_Holo.json

Usage:
    python3 download_ebay_images.py

    Configure ITEM_ID, CARD_NUMBERS, and SET_NAME at the bottom.
    Use CARD_NUMBERS = ["all"] to download all cards in a listing.

DB lookup by card_id (from sidecar JSON):
    SELECT * FROM card_master WHERE id = '{full_uuid_from_json}'
"""

import os
import re
import json
import requests
import xml.etree.ElementTree as ET
from datetime import date
from dotenv import load_dotenv

load_dotenv()

from importer.ebay_auth import get_trading_headers, get_user_token, TRADING_API_URL
from utils.ebay_parser import parse_variation_name, infer_set_name_from_title

NS = "urn:ebay:apis:eBLBaseComponents"


# ── XML helpers ───────────────────────────────────────────────────────────────

def _find(node, tag):
    return node.find(f"{{{NS}}}{tag}")

def _text(node, tag, default=None):
    el = _find(node, tag)
    return el.text.strip() if el is not None and el.text else default


# ── DB lookup ─────────────────────────────────────────────────────────────────

def _get_card_from_db(card_number: str, set_name: str) -> dict:
    """
    Look up card in card_master by number + set name.
    Returns full card info including UUID and set_code.
    """
    try:
        from db.connection import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                SELECT cm.id::text AS card_id,
                       cm.name,
                       cm.card_number,
                       cs.set_code,
                       cs.name AS set_name
                FROM card_master cm
                JOIN card_sets cs ON cm.set_id = cs.id
                WHERE cm.card_number = %s
                  AND LOWER(cs.name) = LOWER(%s)
                LIMIT 1
            """, (card_number, set_name))
            row = cur.fetchone()
            if row:
                return {
                    "card_id":  row["card_id"],
                    "set_code": row["set_code"],
                    "set_name": row["set_name"],
                    "found":    True,
                }
    except Exception as e:
        print(f"  ⚠️  DB lookup failed: {e}")
    return {"card_id": None, "set_code": None, "set_name": set_name, "found": False}


def _store_image_path_in_db(card_id: str, image_path: str):
    """Store image path in card_master.image_url_own."""
    try:
        from db.connection import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                UPDATE card_master 
                SET image_url_own = %s
                WHERE id::text = %s
            """, (image_path, card_id))
    except Exception as e:
        print(f"  ⚠️  Could not update image_url_own: {e}")


# ── Filename builder ──────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    """Clean string for use in filename."""
    s = str(s).strip()
    s = re.sub(r'[^\w\s-]', '', s)      # remove special chars
    s = re.sub(r'[\s-]+', '_', s)        # spaces/dashes → underscore
    s = re.sub(r'_+', '_', s)            # collapse multiple underscores
    return s.strip('_')


def _build_filename(set_code: str, card_number: str, card_name: str,
                    variant_type: str, source_type: str, ext: str) -> str:
    """
    Build professional filename.

    Format: {set_id}_{number}_{name}_{variant}.ext
            {set_id}_{number}_{name}_{variant}_{source}.ext  (if source present)

    Examples:
        sv8_013_Roller_Reverse_Holo.jpg
        sv4_049_Golisopod_Normal_Deck_Exclusive.jpg
        svp_044_Charmander_Normal_Promo.jpg
    """
    parts = [
        _clean(set_code),
        str(card_number).zfill(3),
        _clean(card_name),
        _clean(variant_type),
    ]

    # Add source only if present
    if source_type:
        # Convert snake_case to Title_Case: deck_exclusive → Deck_Exclusive
        source_clean = _clean(source_type.replace('_', ' ').title())
        parts.append(source_clean)

    return "_".join(parts) + f".{ext}"


# ── Main downloader ───────────────────────────────────────────────────────────

def download_images_for_cards(item_id: str, card_numbers: list[str],
                               set_name: str = ""):
    """
    Download images for specific card numbers from an eBay variation listing.

    Args:
        item_id:      eBay listing ID
        card_numbers: Card numbers to download e.g. ['13', '91', '159', '160']
                      Pass ['all'] to download everything.
        set_name:     Set name for DB lookup e.g. 'Surging Sparks'
                      Auto-detected from listing title if not provided.

    For each card downloads:
        1. High-res image (1200x1600px)
        2. Sidecar JSON with full UUID and metadata
        3. Updates card_master.image_url_own in DB
    """
    token = get_user_token()

    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
  <IncludeItemSpecifics>true</IncludeItemSpecifics>
</GetItemRequest>"""

    print(f"📦 Fetching listing {item_id}...")
    resp = requests.post(
        TRADING_API_URL,
        headers=get_trading_headers("GetItem"),
        data=xml_body.encode("utf-8"),
        timeout=30,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    item = _find(root, "Item")
    if item is None:
        print("❌ Item not found.")
        return

    # Auto-detect set name from title if not provided
    if not set_name:
        title    = _text(item, "Title", "")
        set_name = infer_set_name_from_title(title) or ""
        if set_name:
            print(f"📋 Auto-detected set: {set_name}")
        else:
            print("⚠️  Could not detect set name — DB lookup may fail")

    # ── Find all VariationSpecificPictureSet ──────────────────────────────────
    pic_sets = list(root.iter(f"{{{NS}}}VariationSpecificPictureSet"))
    if not pic_sets:
        print("❌ No VariationSpecificPictureSet found in this listing.")
        return

    print(f"✅ Found {len(pic_sets)} variation picture sets")
    print(f"📥 Downloading cards: {card_numbers}\n")

    downloaded = 0
    skipped    = 0
    failed     = 0

    for pic_set in pic_sets:
        spec_value = _text(pic_set, "VariationSpecificValue", "")
        if not spec_value:
            continue

        # Extract card number from variation name
        num_match = re.match(r'^(?:SVP\s*)?0*(\d+)', spec_value, re.IGNORECASE)
        if not num_match:
            continue
        card_num = num_match.group(1) or "0"

        # Filter to requested cards
        if card_numbers != ["all"] and card_num not in card_numbers:
            continue

        # Get picture URL
        pic_url = None
        for el in pic_set.iter(f"{{{NS}}}PictureURL"):
            pic_url = el.text
            break

        if not pic_url:
            print(f"  ⚠️  No image URL for card #{card_num}")
            failed += 1
            continue

        # Parse variation name
        parsed       = parse_variation_name(spec_value)
        if card_num == "159" and "SVP" in spec_value:
            print(f"  DEBUG: set_override={parsed.get('set_override')} | spec_value={spec_value}")
        card_name    = parsed.get("card_name") or spec_value
        variant_type = parsed.get("variant_type", "Normal")
        source_type  = parsed.get("source_type")

        # Use set_override from parser if available (e.g. SVP promos)
        lookup_set = parsed.get("set_override") or set_name
        if card_num == "159" and "SVP" in spec_value:
            print(f"  DEBUG lookup_set={lookup_set}")
        db_info    = _get_card_from_db(card_num, lookup_set)
        if card_num == "159" and "SVP" in spec_value:
            print(f"  DEBUG db_info={db_info}")
        set_code = db_info.get("set_code") or "unknown"
        card_id  = db_info.get("card_id")

        if not db_info["found"]:
            print(f"  ⚠️  #{card_num} {card_name} not found in DB — using set_code='{set_code}'")

        # Determine file extension
        ext = "png" if ".PNG" in pic_url.upper() else "jpg"

        # Build filename
        filename     = _build_filename(set_code, card_num, card_name,
                                       variant_type, source_type, ext)
        json_name    = filename.rsplit('.', 1)[0] + ".json"

        # Output directory organized by set_code
        output_dir   = os.path.join("images", "ebay", set_code)
        os.makedirs(output_dir, exist_ok=True)

        image_path   = os.path.join(output_dir, filename)
        json_path    = os.path.join(output_dir, json_name)

        # ── Download image ────────────────────────────────────────────────────
        if os.path.exists(image_path):
            print(f"  ⏭  #{card_num} already exists — {filename}")
            skipped += 1
            continue

        print(f"  ⬇️  #{card_num} {spec_value[:50]}")

        success = _download_image(pic_url, image_path)

        if not success:
            failed += 1
            continue

        downloaded += 1

        # ── Write sidecar JSON ────────────────────────────────────────────────
        sidecar = {
            "card_id":      card_id,
            "card_name":    card_name,
            "card_number":  card_num,
            "set_code":     set_code,
            "set_name":     set_name,
            "variant_type": variant_type,
            "source_type":  source_type,
            "ebay_item_id": item_id,
            "image_path":   image_path,
            "downloaded_at": str(date.today()),
        }
        with open(json_path, "w") as f:
            json.dump(sidecar, f, indent=2)
        print(f"       → {filename}")
        print(f"       → {json_name} (sidecar)")

        # ── Update card_master.image_url_own in DB ────────────────────────────
        if card_id:
            _store_image_path_in_db(card_id, image_path)
            print(f"       → DB updated: image_url_own")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"✅ Downloaded: {downloaded}")
    print(f"⏭  Skipped:    {skipped}")
    print(f"❌ Failed:      {failed}")
    print(f"📁 Images:     images/ebay/{set_code}/")


def _download_image(url: str, filepath: str) -> bool:
    """
    Download image at highest resolution.
    Tries s-l1600 first, falls back to original URL.
    Returns True if successful.
    """
    # Build high-res URL from eBay image hash
    hash_match = re.search(r'/z/([^/]+)/\$_', url)
    if hash_match:
        img_hash = hash_match.group(1)
        hq_url   = f"https://i.ebayimg.com/images/g/{img_hash}/s-l1600.jpg"
    else:
        hq_url = url.replace("$_57.", "$_1.")

    for attempt_url in [hq_url, url]:
        try:
            r = requests.get(attempt_url, timeout=15)
            r.raise_for_status()
            with open(filepath, "wb") as f:
                f.write(r.content)
            size_kb = len(r.content) / 1024
            res_note = "" if attempt_url == hq_url else " [fallback]"
            print(f"       ({size_kb:.0f} KB{res_note})")
            return True
        except Exception:
            continue

    print(f"  ❌ Failed to download — all attempts failed")
    return False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # ── Configure here ────────────────────────────────────────────────────────
    ITEM_ID      = "335662210469"              # eBay listing ID
    CARD_NUMBERS = ["13", "91", "159", "160"]  # or ["all"] for everything
    SET_NAME     = "Surging Sparks"            # leave "" to auto-detect
    # ─────────────────────────────────────────────────────────────────────────

    download_images_for_cards(ITEM_ID, CARD_NUMBERS, SET_NAME)
