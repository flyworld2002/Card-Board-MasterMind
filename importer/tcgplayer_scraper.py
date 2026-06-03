"""
importer/tcgplayer_scraper.py
Scrapes TCGPlayer order history using Selenium + your existing Firefox profile.
No password needed — uses your existing logged-in Firefox session.

Requirements:
    pip3 install selenium
    brew install geckodriver

Usage:
    python3 main.py --scrape-tcgplayer
    python3 main.py --scrape-tcgplayer --days 30 --dry-run
"""

import time
import re
from datetime import datetime, timezone, timedelta

try:
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

from db.connection import (
    get_game_id, get_or_create_set, find_card_by_external_id,
    insert_card_master, insert_card_attributes,
    insert_purchase, insert_inventory
)
from utils.pokemon_api import (
    search_cards, parse_card_master_fields, parse_card_attribute_fields
)

ORDER_HISTORY_URL = "https://www.tcgplayer.com/myaccount/orderhistory"

# TCGPlayer condition strings → our standard
CONDITION_MAP = {
    "near mint":                  "Near Mint",
    "near mint holofoil":         "Near Mint",
    "lightly played":             "Lightly Played",
    "lightly played holofoil":    "Lightly Played",
    "moderately played":          "Moderately Played",
    "moderately played holofoil": "Moderately Played",
    "heavily played":             "Heavily Played",
    "heavily played holofoil":    "Heavily Played",
    "damaged":                    "Damaged",
    "damaged holofoil":           "Damaged",
    "nm":                         "Near Mint",
    "lp":                         "Lightly Played",
    "mp":                         "Moderately Played",
    "hp":                         "Heavily Played",
}

# TCGPlayer order number pattern e.g. 27F1EFEA-ECE5B5-D3528
ORDER_NUM_RE = re.compile(r'[A-F0-9]{8}-[A-F0-9]{6}-[A-F0-9]{5}')


# ----------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------

def scrape_tcgplayer_orders(days: int = 30, dry_run: bool = False,
                            verbose: bool = True) -> dict:
    if not SELENIUM_AVAILABLE:
        print("\n❌ Selenium not installed. Run:")
        print("   pip3 install selenium")
        print("   brew install geckodriver\n")
        return {}

    _print(verbose, f"\n=== TCGPlayer Order Scraper (last {days} days) ===\n")
    _print(verbose, "Opening Firefox with your existing profile...")
    _print(verbose, "(A Firefox window will open — don't close it)\n")

    driver = _build_driver()
    if not driver:
        return {}

    try:
        cutoff  = datetime.now(timezone.utc) - timedelta(days=days)
        game_id = get_game_id("Pokemon")

        # Load order history page
        driver.get(ORDER_HISTORY_URL)
        _wait_for_orders(driver)

        page_text = driver.find_element(By.TAG_NAME, "body").text
        _print(verbose, f"✓ Page loaded\n")

        # Find all order numbers on the page
        order_numbers = list(dict.fromkeys(ORDER_NUM_RE.findall(page_text)))
        if not order_numbers:
            _print(verbose, "No order numbers found on page.")
            return {"imported": 0, "skipped": 0, "flagged": []}

        _print(verbose, f"Found {len(order_numbers)} order(s). Fetching details...\n")

        imported = 0
        skipped  = 0
        flagged  = []

        for order_num in order_numbers:
            url = f"https://www.tcgplayer.com/myaccount/orderhistory/{order_num}"
            driver.get(url)
            _wait_for_orders(driver)

            body_text = driver.find_element(By.TAG_NAME, "body").text
            order     = _parse_order_from_text(order_num, body_text)

            if not order:
                _print(verbose, f"  Could not parse order {order_num}, skipping.")
                skipped += 1
                continue

            if order['date'] < cutoff:
                _print(verbose, f"  Order {order_num} is older than {days} days, stopping.")
                break

            _print(verbose, f"Order {order_num} — {order['date'].strftime('%Y-%m-%d')} — {len(order['items'])} item(s)")

            if not order['items']:
                skipped += 1
                continue

            total_cost  = sum(i['price'] * i['quantity'] for i in order['items'])
            purchase_id = None

            if not dry_run:
                purchase_id = insert_purchase(
                    source        = "tcgplayer",
                    purchase_type = "lot" if len(order['items']) > 1 else "single",
                    reference_id  = order_num,
                    total_cost    = total_cost,
                    card_count    = sum(i['quantity'] for i in order['items']),
                    purchased_at  = order['date'],
                )

            for item in order['items']:
                result = _process_item(item, game_id, purchase_id, dry_run, verbose)
                if result == "imported":
                    imported += 1
                elif result == "skipped":
                    skipped += 1
                elif isinstance(result, dict):
                    flagged.append(result)

            time.sleep(1)

        _print(verbose, f"\n{'[DRY RUN] ' if dry_run else ''}Complete.")
        _print(verbose, f"Imported: {imported} | Skipped: {skipped} | Flagged: {len(flagged)}")

        if flagged:
            _print(verbose, f"\n--- {len(flagged)} item(s) flagged for manual review ---")
            for i, f in enumerate(flagged, 1):
                _print(verbose, f"  {i}. {f['card_name']} — {f['reason']}")
                for m in f.get("matches", []):
                    _print(verbose, f"     → [{m['id']}] {m['name']} #{m.get('card_number')} ({m.get('variant','—')})")
            _print(verbose, "\nRun  python3 main.py --manual  to resolve flagged items.")

        return {"imported": imported, "skipped": skipped, "flagged": flagged}

    finally:
        driver.quit()


# ----------------------------------------------------------------
# Page text parser — matches TCGPlayer's exact order detail format
#
# Format observed:
#   Card Name (Variant)
#   Set Name
#   Rarity: X
#   Condition: Near Mint Holofoil
#   $price
#   qty
# ----------------------------------------------------------------

def _parse_order_from_text(order_num: str, text: str) -> dict | None:
    """Parse order date and items from the body text of an order detail page."""

    # Extract order date
    date_match = re.search(
        r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
        r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|'
        r'Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}',
        text
    )
    order_date = _parse_date(date_match.group(0)) if date_match else datetime.now(timezone.utc)

    # Parse items
    items = _parse_items(text)

    return {"number": order_num, "date": order_date, "items": items}


def _parse_items(text: str) -> list[dict]:
    """
    Parse card items from order detail page text.

    TCGPlayer order detail text format (one item):
        Beartic (Master Ball Pattern)
        SV: Black Bolt
        Rarity: Rare
        Condition: Near Mint Holofoil
        $8.75
        2
    """
    items  = []
    lines  = [l.strip() for l in text.split("\n") if l.strip()]

    i = 0
    while i < len(lines):
        # Look for a price line: $X.XX
        if re.match(r'^\$[\d]+\.[\d]{2}$', lines[i]):
            price = float(lines[i].replace("$", ""))

            # Quantity is the next non-empty line after price
            qty = 1
            if i + 1 < len(lines):
                qty_match = re.match(r'^(\d+)$', lines[i+1])
                if qty_match:
                    qty = int(qty_match.group(1))

            # Look backwards from price to find card name, set, condition
            card_name = None
            set_name  = ""
            condition = "Near Mint"

            for j in range(i - 1, max(i - 8, -1), -1):
                line = lines[j]
                line_lower = line.lower()

                # Condition line: "Condition: Near Mint Holofoil"
                if line_lower.startswith("condition:"):
                    raw = line.split(":", 1)[1].strip().lower()
                    condition = CONDITION_MAP.get(raw, "Near Mint")

                # Rarity line — skip
                elif line_lower.startswith("rarity:"):
                    continue

                # Set name: starts with "SV:", "XY:", "BW:", "EX:", "HS:",
                # "Base", "Jungle", "Fossil", etc.
                elif (re.match(r'^(SV|XY|BW|EX|HS|SWSH|SM|POP|DP|GS|RS|E\s):', line) or
                      re.match(r'^(Base Set|Jungle|Fossil|Team Rocket|Gym|Neo|Legend|Classic)', line, re.I) or
                      re.match(r'^(Scarlet|Violet|Sun|Moon|Sword|Shield|Black|White|Diamond|Pearl|HeartGold|SoulSilver)', line, re.I)):
                    set_name = line

                # Card name — first meaningful text line going backwards
                # Skip lines that are metadata
                elif (not line_lower.startswith(("condition:", "rarity:", "items", "details",
                                                  "price", "quantity", "shipped", "order",
                                                  "rate transaction", "viewing")) and
                      len(line) > 2 and
                      not re.match(r'^\$[\d]', line) and
                      not re.match(r'^\d+$', line)):
                    if card_name is None:
                        card_name = line

            # Filter out order summary lines mistaken for items
            SKIP = ("subtotal:", "sales tax", "total:", "shipping:", "order total")
            if card_name and price > 0 and not any(
                    s in card_name.lower() for s in SKIP):
                items.append({
                    "card_name": card_name,
                    "set_name":  set_name,
                    "condition": condition,
                    "quantity":  qty,
                    "price":     price,
                })

        i += 1

    return items


# ----------------------------------------------------------------
# Selenium driver — uses your existing Firefox profile
# ----------------------------------------------------------------

def _build_driver():
    """Launch Firefox using your default profile (already logged into TCGPlayer)."""
    import glob, os

    options = Options()
    profile = _find_firefox_profile()
    if profile:
        options.add_argument("-profile")
        options.add_argument(profile)
    else:
        print("Warning: Firefox profile not found. You may need to log in manually.")

    options.headless = False

    try:
        driver = webdriver.Firefox(options=options)
        driver.set_window_size(1280, 900)
        return driver
    except Exception as e:
        print(f"\n❌ Could not start Firefox: {e}")
        print("Make sure geckodriver is installed:  brew install geckodriver\n")
        return None


def _find_firefox_profile() -> str | None:
    import glob, os
    base = os.path.expanduser("~/Library/Application Support/Firefox/Profiles")
    if not os.path.exists(base):
        return None
    for pattern in ["*.default-release", "*.default"]:
        matches = glob.glob(os.path.join(base, pattern))
        if matches:
            return matches[0]
    dirs = [p for p in os.listdir(base) if os.path.isdir(os.path.join(base, p))]
    return os.path.join(base, dirs[0]) if dirs else None


def _wait_for_orders(driver):
    """Wait for the page to fully render."""
    try:
        WebDriverWait(driver, 20).until(
            lambda d: len(d.find_element(By.TAG_NAME, "body").text) > 500
        )
    except TimeoutException:
        pass
    time.sleep(3)


# ----------------------------------------------------------------
# Card resolution + DB insert
# ----------------------------------------------------------------

def _process_item(item: dict, game_id: str, purchase_id,
                  dry_run: bool, verbose: bool):
    card_id = _resolve_card(item, game_id, dry_run, verbose)
    if card_id is None:
        return "skipped"
    if isinstance(card_id, dict):
        return card_id
    _print(verbose, f"  ✓ {item['quantity']}x {item['card_name']} "
           f"[{item['condition']}] @ ${item['price']:.2f}")
    if not dry_run:
        insert_inventory(
            card_id     = card_id,
            purchase_id = purchase_id,
            condition   = item['condition'],
            quantity    = item['quantity'],
            cost_basis  = item['price'],
            acquired_at = datetime.now(timezone.utc),
        )
    return "imported"


def _resolve_card(item: dict, game_id: str, dry_run: bool, verbose: bool):
    results = search_cards(name=item['card_name'], set_name=item.get('set_name'))

    if not results:
        _print(verbose, f"  ✗ Not found in API: {item['card_name']}")
        return {"card_name": item['card_name'], "set_name": item.get('set_name', ''),
                "reason": "No match in PokemonTCG API", "matches": []}

    if len(results) > 1:
        matches = [{"id": c["id"], "name": c["name"],
                    "card_number": c.get("number"),
                    "variant": ", ".join(c.get("subtypes", []))} for c in results]
        _print(verbose, f"  ? Ambiguous ({len(results)}): {item['card_name']}")
        return {"card_name": item['card_name'], "set_name": item.get('set_name', ''),
                "reason": f"{len(results)} matches — run --manual to resolve",
                "matches": matches}

    api_card = results[0]
    existing = find_card_by_external_id(api_card["id"])
    if existing:
        return str(existing["id"])

    if dry_run:
        _print(verbose, f"  [dry run] Would create: {api_card['name']} #{api_card.get('number')}")
        return "dry-run-id"

    fields      = parse_card_master_fields(api_card)
    attr_fields = parse_card_attribute_fields(api_card)
    set_id = get_or_create_set(
        game_id=game_id, name=fields["set_name"], set_code=fields["set_code"],
        series=fields.get("series"), release_year=fields.get("release_year"),
        total_cards=fields.get("total_cards"),
    )
    card_id = insert_card_master(
        set_id=set_id, name=fields["name"], card_number=fields["card_number"],
        rarity=fields.get("rarity"), variant=fields.get("variant"),
        finish=fields.get("finish"), is_promo=fields.get("is_promo", False),
        is_first_edition=fields.get("is_first_edition", False),
        image_url=fields.get("image_url"), external_id=fields["external_id"],
    )
    insert_card_attributes(card_id, **attr_fields)
    return card_id


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

def _parse_date(date_str: str) -> datetime:
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y",
                "%b %d %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _print(verbose: bool, msg: str):
    if verbose:
        print(msg)
