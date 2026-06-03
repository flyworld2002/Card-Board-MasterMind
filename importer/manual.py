"""
importer/manual.py
Interactive CLI for manually entering purchases
(eBay, local shop, card shows, trades, etc.)
"""

from datetime import datetime, timezone
from db.connection import (
    get_game_id, get_or_create_set, find_card_by_name_set,
    insert_card_master, insert_card_attributes,
    insert_purchase, insert_inventory
)
from utils.pokemon_api import (
    search_cards, parse_card_master_fields, parse_card_attribute_fields
)

SOURCES = ["tcgplayer", "ebay", "local_shop", "card_show", "trade", "other"]
CONDITIONS = ["Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged"]


def manual_import():
    """Interactive manual purchase entry."""
    print("\n=== Manual Card Import ===\n")

    # Purchase-level info
    source       = _choose("Purchase source", SOURCES)
    purchase_type = _choose("Purchase type", ["single", "lot", "collection"])
    reference_id = _ask("Reference / order ID (optional)", required=False)
    date_str     = _ask("Purchase date (YYYY-MM-DD, leave blank for today)", required=False)
    purchased_at = _parse_date(date_str) if date_str else datetime.now(timezone.utc)
    notes        = _ask("Notes (optional)", required=False)

    cards_entered = []

    print("\n--- Enter cards (type 'done' when finished) ---\n")
    while True:
        card_name = _ask("Card name (or 'done')", required=False)
        if card_name.lower() in ("done", ""):
            break

        set_name  = _ask("Set name")
        condition = _choose("Condition", CONDITIONS)
        quantity  = int(_ask("Quantity", default="1") or 1)
        price     = float(_ask("Price paid per card (USD)") or 0)

        # Look up card in API
        card_id = _resolve_card_interactive(card_name, set_name)
        if card_id:
            cards_entered.append({
                "card_id":   card_id,
                "condition": condition,
                "quantity":  quantity,
                "price":     price,
            })
            print(f"  ✓ Added: {card_name} [{condition}] x{quantity} @ ${price:.2f}\n")
        else:
            retry = _ask("Skip this card? (y/n)", default="y")
            if retry.lower() != "y":
                print("  Card skipped.\n")

    if not cards_entered:
        print("No cards entered. Exiting.")
        return

    # Summary before committing
    total_cost = sum(c["price"] * c["quantity"] for c in cards_entered)
    print(f"\n--- Summary ---")
    print(f"Source:     {source}")
    print(f"Date:       {purchased_at.strftime('%Y-%m-%d')}")
    print(f"Cards:      {len(cards_entered)} unique card(s), "
          f"{sum(c['quantity'] for c in cards_entered)} total copies")
    print(f"Total cost: ${total_cost:.2f}")
    confirm = _ask("\nCommit to database? (y/n)", default="y")
    if confirm.lower() != "y":
        print("Cancelled. Nothing saved.")
        return

    purchase_id = insert_purchase(
        source        = source,
        purchase_type = purchase_type,
        reference_id  = reference_id or None,
        total_cost    = total_cost,
        card_count    = sum(c["quantity"] for c in cards_entered),
        notes         = notes or None,
        purchased_at  = purchased_at,
    )

    for c in cards_entered:
        insert_inventory(
            card_id     = c["card_id"],
            purchase_id = purchase_id,
            condition   = c["condition"],
            quantity    = c["quantity"],
            cost_basis  = c["price"],
            acquired_at = purchased_at,
        )

    print(f"\n✓ Saved {len(cards_entered)} card(s) under purchase {purchase_id}")


# ----------------------------------------------------------------
# Card resolution with interactive disambiguation
# ----------------------------------------------------------------

def _resolve_card_interactive(card_name: str, set_name: str) -> str | None:
    """Search API, handle ambiguous results interactively."""
    print(f"  Searching PokemonTCG API for '{card_name}' in '{set_name}'...")
    results = search_cards(name=card_name, set_name=set_name)

    if not results:
        print(f"  ✗ No results found.")
        return None

    if len(results) == 1:
        card = results[0]
        print(f"  Found: {card['name']} #{card.get('number')} ({card['set']['name']})")
        confirm = _ask("  Use this card? (y/n)", default="y")
        if confirm.lower() != "y":
            return None
        return _get_or_create_card(card)

    # Multiple matches — show list and let user pick
    print(f"  Found {len(results)} possible matches:")
    for i, c in enumerate(results, 1):
        subtypes = ", ".join(c.get("subtypes", [])) or "—"
        print(f"    {i}. {c['name']} #{c.get('number')} | "
              f"{c['set']['name']} | {c.get('rarity')} | {subtypes}")

    choice = _ask(f"  Pick a number (1-{len(results)}) or 's' to skip", default="s")
    if choice.lower() == "s" or not choice.isdigit():
        return None

    idx = int(choice) - 1
    if 0 <= idx < len(results):
        return _get_or_create_card(results[idx])

    return None


def _get_or_create_card(api_card: dict) -> str:
    """Get existing card_master entry or create a new one."""
    from db.connection import find_card_by_external_id
    existing = find_card_by_external_id(api_card["id"])
    if existing:
        return str(existing["id"])

    game_id    = get_game_id("Pokemon")
    fields     = parse_card_master_fields(api_card)
    attr_fields = parse_card_attribute_fields(api_card)

    set_id = get_or_create_set(
        game_id      = game_id,
        name         = fields["set_name"],
        set_code     = fields["set_code"],
        series       = fields.get("series"),
        release_year = fields.get("release_year"),
        total_cards  = fields.get("total_cards"),
    )

    card_id = insert_card_master(
        set_id           = set_id,
        name             = fields["name"],
        card_number      = fields["card_number"],
        rarity           = fields.get("rarity"),
        variant          = fields.get("variant"),
        finish           = fields.get("finish"),
        is_promo         = fields.get("is_promo", False),
        is_first_edition = fields.get("is_first_edition", False),
        image_url        = fields.get("image_url"),
        external_id      = fields["external_id"],
    )
    insert_card_attributes(card_id, **attr_fields)
    return card_id


# ----------------------------------------------------------------
# CLI helpers
# ----------------------------------------------------------------

def _ask(prompt: str, default: str = "", required: bool = True) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        val = input(f"  {prompt}{hint}: ").strip()
        if not val and default:
            return default
        if val or not required:
            return val
        print("  Required — please enter a value.")


def _choose(prompt: str, options: list[str]) -> str:
    print(f"  {prompt}:")
    for i, o in enumerate(options, 1):
        print(f"    {i}. {o}")
    while True:
        val = input("  Pick a number: ").strip()
        if val.isdigit() and 1 <= int(val) <= len(options):
            return options[int(val) - 1]
        print(f"  Enter a number between 1 and {len(options)}.")


def _parse_date(date_str: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    print(f"  Could not parse date '{date_str}', using today.")
    return datetime.now(timezone.utc)
