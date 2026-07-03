"""
importer/ebay_image_downloader.py
Download eBay listing images with professional naming convention.

Filename format:
    {set_id}_{card_number}_{Card_Name}_{Variant}.jpg
    {set_id}_{card_number}_{Card_Name}_{Variant}_{Source}.jpg  ← if source present

Examples:
    sv8_013_Roller_Reverse_Holo.jpg
    sv4_049_Golisopod_Normal_Deck_Exclusive.jpg
    svp_159_Magneton_Normal.png

Storage structure:
    images/
        ebay/
            {item_id}_{Set_Name}_{Label}/     ← images only
        metadata/
            {item_id}_{Set_Name}_{Label}/     ← sidecar JSONs only

Usage:
    # Download one card
    python3 download_ebay_images.py --item 335662210469 --cards 13

    # Download multiple cards
    python3 download_ebay_images.py --item 335662210469 --cards 13 91 159 160

    # Download all cards
    python3 download_ebay_images.py --item 335662210469 --all

    # With set name and label
    python3 download_ebay_images.py --item 335662210469 --all --set "Surging Sparks" --label "RH_Ultra_Rare"

    # Full example
    python3 download_ebay_images.py \\
        --item 335777076705 \\
        --all \\
        --set "Prismatic Evolutions" \\
        --label "Pokeball_Holo"
"""

import os
import re
import json
import argparse
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


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_card_from_db(card_number: str, set_name: str) -> dict:
    """Look up card in card_master by number + set name."""
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


# ── Filename / folder builders ────────────────────────────────────────────────

def _clean(s: str) -> str:
    s = str(s).strip()
    s = re.sub(r'[^\w\s-]', '', s)
    s = re.sub(r'[\s-]+', '_', s)
    s = re.sub(r'_+', '_', s)
    return s.strip('_')


def _build_filename(set_code: str, card_number: str, card_name: str,
                    variant_type: str, source_type: str, ext: str) -> str:
    """
    Format: {set_id}_{number}_{name}_{variant}.ext
            {set_id}_{number}_{name}_{variant}_{source}.ext
    """
    parts = [
        _clean(set_code),
        str(card_number).zfill(3),
        _clean(card_name),
        _clean(variant_type),
    ]
    if source_type:
        parts.append(_clean(source_type.replace('_', ' ').title()))
    return "_".join(parts) + f".{ext}"


def _build_folder_name(item_id: str, set_name: str, label: str) -> str:
    """Format: {item_id}_{Set_Name}_{Label}"""
    parts = [item_id, _clean(set_name)]
    if label:
        parts.append(_clean(label))
    return "_".join(filter(None, parts))


# ── Main downloader ───────────────────────────────────────────────────────────

def download_images_for_cards(item_id: str, card_numbers: list[str],
                               set_name: str = "", label: str = ""):
    """
    Download images for specific card numbers from an eBay variation listing.

    Args:
        item_id:      eBay listing ID
        card_numbers: List of card numbers, or ['all'] for everything
        set_name:     Set name e.g. 'Surging Sparks' (auto-detected if blank)
        label:        Short descriptor e.g. 'RH', 'Commons', 'Pokeball_Holo'
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

    # Auto-detect set name if not provided
    if not set_name:
        title    = _text(item, "Title", "")
        set_name = infer_set_name_from_title(title) or ""
        if set_name:
            print(f"📋 Auto-detected set: {set_name}")
        else:
            print("⚠️  Could not detect set name")

    # Build folder names
    folder_name  = _build_folder_name(item_id, set_name, label)
    output_dir   = os.path.join("images", "ebay", folder_name)
    metadata_dir = os.path.join("images", "metadata", folder_name)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(metadata_dir, exist_ok=True)

    print(f"📁 Images:   {output_dir}/")
    print(f"📁 Metadata: {metadata_dir}/\n")

    # Find all variation picture sets
    pic_sets = list(root.iter(f"{{{NS}}}VariationSpecificPictureSet"))
    if not pic_sets:
        print("❌ No VariationSpecificPictureSet found.")
        return

    print(f"✅ Found {len(pic_sets)} variation picture sets")
    if card_numbers == ["all"]:
        print(f"📥 Downloading: ALL cards\n")
    else:
        print(f"📥 Downloading cards: {card_numbers}\n")

    downloaded = 0
    skipped    = 0
    failed     = 0

    for pic_set in pic_sets:
        spec_value = _text(pic_set, "VariationSpecificValue", "")
        if not spec_value:
            continue

        # Extract card number
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
        card_name    = parsed.get("card_name") or spec_value
        # Build a filename-friendly variant string from the seven axes
        _axis_vals = [parsed.get(k) for k in
                      ("foil_type", "foil_pattern", "texture", "material", "size")]
        variant_type = "_".join(v for v in _axis_vals if v) or "non_holo"
        source_type  = parsed.get("source_type")

        # DB lookup — use set_override for promos (e.g. SVP cards in SV listings)
        lookup_set = parsed.get("set_override") or set_name
        db_info    = _get_card_from_db(card_num, lookup_set)
        set_code   = db_info.get("set_code") or "unknown"
        card_id    = db_info.get("card_id")

        if not db_info["found"]:
            print(f"  ⚠️  #{card_num} {card_name} not in DB — set_code=unknown")

        # Build filename and paths
        ext        = "png" if ".PNG" in pic_url.upper() else "jpg"
        filename   = _build_filename(set_code, card_num, card_name,
                                     variant_type, source_type, ext)
        json_name  = filename.rsplit('.', 1)[0] + ".json"
        image_path = os.path.join(output_dir, filename)
        json_path  = os.path.join(metadata_dir, json_name)

        # Skip if already exists
        if os.path.exists(image_path):
            print(f"  ⏭  #{card_num} already exists — {filename}")
            skipped += 1
            continue

        print(f"  ⬇️  #{card_num} {spec_value[:55]}")

        if not _download_image(pic_url, image_path):
            failed += 1
            continue

        downloaded += 1
        print(f"       → {filename}")

        # Write sidecar JSON
        sidecar = {
            "card_id":       card_id,
            "card_name":     card_name,
            "card_number":   card_num,
            "set_code":      set_code,
            "set_name":      lookup_set,
            "variant_type":  variant_type,
            "source_type":   source_type,
            "ebay_item_id":  item_id,
            "image_path":    image_path,
            "downloaded_at": str(date.today()),
        }
        with open(json_path, "w") as f:
            json.dump(sidecar, f, indent=2)
        print(f"       → {json_name} (sidecar)")

        # Update DB
        if card_id:
            _store_image_path_in_db(card_id, image_path)
            print(f"       → DB updated: image_url_own")

    print(f"\n{'─'*60}")
    print(f"✅ Downloaded: {downloaded}")
    print(f"⏭  Skipped:    {skipped}")
    print(f"❌ Failed:      {failed}")
    print(f"📁 Images:     {output_dir}/")
    print(f"📁 Metadata:   {metadata_dir}/")


def _download_image(url: str, filepath: str) -> bool:
    """Download image at highest resolution. Returns True if successful."""
    hash_match = re.search(r'/z/([^/]+)/\$_', url)
    hq_url     = (f"https://i.ebayimg.com/images/g/{hash_match.group(1)}/s-l1600.jpg"
                  if hash_match else url.replace("$_57.", "$_1."))

    for attempt_url in [hq_url, url]:
        try:
            r = requests.get(attempt_url, timeout=15)
            r.raise_for_status()
            with open(filepath, "wb") as f:
                f.write(r.content)
            size_kb = len(r.content) / 1024
            note    = "" if attempt_url == hq_url else " [fallback]"
            print(f"       ({size_kb:.0f} KB{note})")
            return True
        except Exception:
            continue

    print(f"  ❌ All download attempts failed")
    return False


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download eBay listing images",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download one card
  python3 download_ebay_images.py --item 335662210469 --cards 13

  # Download multiple cards
  python3 download_ebay_images.py --item 335662210469 --cards 13 91 159 160

  # Download all cards
  python3 download_ebay_images.py --item 335662210469 --all

  # With set name and label
  python3 download_ebay_images.py --item 335662210469 --all --set "Surging Sparks" --label "RH_Ultra_Rare"

  # Prismatic Evolutions Pokeball Holo listing
  python3 download_ebay_images.py --item 335777076705 --all --set "Prismatic Evolutions" --label "Pokeball_Holo"
        """
    )

    parser.add_argument("--item",   required=True,  metavar="ITEM_ID",
                        help="eBay listing ID")
    parser.add_argument("--cards",  nargs="+",       metavar="NUM",
                        help="Card number(s) to download e.g. --cards 13 91 159")
    parser.add_argument("--all",    action="store_true",
                        help="Download all cards in the listing")
    parser.add_argument("--set",    default="",      metavar="SET_NAME",
                        help="Set name e.g. 'Surging Sparks' (auto-detected if omitted)")
    parser.add_argument("--label",  default="",      metavar="LABEL",
                        help="Short folder label e.g. RH, Commons, Pokeball_Holo")

    args = parser.parse_args()

    if not args.cards and not args.all:
        parser.error("Specify --cards or --all")

    card_numbers = ["all"] if args.all else args.cards

    download_images_for_cards(
        item_id      = args.item,
        card_numbers = card_numbers,
        set_name     = args.set,
        label        = args.label,
    )


if __name__ == "__main__":
    main()
