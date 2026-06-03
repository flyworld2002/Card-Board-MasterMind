"""
importer/image_upload.py
Handles the --upload-image command.
Looks up card by ID or interactively, processes image, uploads to R2,
and saves the URL to card_master.image_url_own.
"""

import sys
from pathlib import Path
from db.connection import db_cursor
from utils.image_processor import process_card_image
from utils.r2_storage import upload_card_image, delete_card_image


def upload_own_image(image_path: str, card_id: str = None,
                     search_name: str = None):
    """
    Main entry point for --upload-image command.

    Args:
        image_path:  Path to the image file on disk.
        card_id:     UUID of the card_master row (optional).
        search_name: Card name to search if card_id not provided.
    """
    path = Path(image_path)
    if not path.exists():
        print(f"Error: file not found — {image_path}")
        sys.exit(1)

    # Resolve which card this image is for
    if not card_id:
        card_id = _pick_card_interactive(search_name)
        if not card_id:
            print("No card selected. Exiting.")
            return

    # Fetch current card info
    card = _get_card(card_id)
    if not card:
        print(f"Error: card not found with ID {card_id}")
        sys.exit(1)

    print(f"\nCard:  {card['name']} #{card['card_number']} — {card['set_name']}")
    if card["image_url_own"]:
        print(f"Current own photo: {card['image_url_own']}")
        confirm = input("  Replace existing photo? (y/n) [n]: ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return

    # Process image (resize + WebP conversion)
    print(f"\nProcessing {path.name}...")
    image_bytes, suggested_name = process_card_image(path)

    # Upload to R2
    print("Uploading to Cloudflare R2...")
    new_url = upload_card_image(
        image_bytes = image_bytes,
        filename    = suggested_name,
        card_id     = card_id,
    )

    # Delete old R2 image if it was our own (not the API stock image)
    if card["image_url_own"]:
        delete_card_image(card["image_url_own"])

    # Save new URL to database
    _save_own_image_url(card_id, new_url)

    print(f"\n✓ Done. {card['name']} own photo updated.")
    print(f"  URL: {new_url}")


def _pick_card_interactive(search_name: str = None) -> str | None:
    """Search for a card interactively and return its ID."""
    if not search_name:
        search_name = input("  Search card name: ").strip()
        if not search_name:
            return None

    results = _search_cards(search_name)

    if not results:
        print(f"  No cards found matching '{search_name}'")
        return None

    if len(results) == 1:
        c = results[0]
        print(f"  Found: {c['name']} #{c['card_number']} — {c['set_name']} ({c['variant'] or 'Standard'})")
        confirm = input("  Use this card? (y/n) [y]: ").strip().lower()
        return str(c["id"]) if confirm != "n" else None

    print(f"\n  Found {len(results)} matches:")
    for i, c in enumerate(results, 1):
        own = "★ has own photo" if c["image_url_own"] else ""
        print(f"    {i}. {c['name']} #{c['card_number']} | "
              f"{c['set_name']} | {c['variant'] or 'Standard'} {own}")

    choice = input(f"  Pick a number (1-{len(results)}) or 's' to skip: ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(results):
        return str(results[int(choice) - 1]["id"])
    return None


def _search_cards(name: str) -> list[dict]:
    with db_cursor() as cur:
        cur.execute("""
            SELECT
                cm.id, cm.name, cm.card_number, cm.variant,
                cm.image_url, cm.image_url_own,
                cs.name AS set_name
            FROM card_master cm
            JOIN card_sets cs ON cm.set_id = cs.id
            WHERE LOWER(cm.name) LIKE LOWER(%s)
            ORDER BY cm.name, cs.name
            LIMIT 20
        """, (f"%{name}%",))
        return cur.fetchall()


def _get_card(card_id: str) -> dict | None:
    with db_cursor() as cur:
        cur.execute("""
            SELECT
                cm.id, cm.name, cm.card_number, cm.variant,
                cm.image_url, cm.image_url_own,
                cs.name AS set_name
            FROM card_master cm
            JOIN card_sets cs ON cm.set_id = cs.id
            WHERE cm.id = %s
        """, (card_id,))
        return cur.fetchone()


def _save_own_image_url(card_id: str, url: str):
    with db_cursor() as cur:
        cur.execute("""
            UPDATE card_master
            SET image_url_own = %s
            WHERE id = %s
        """, (url, card_id))
