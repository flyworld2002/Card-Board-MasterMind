"""
importer/tcgplayer_html.py
Imports TCGPlayer orders from saved HTML files into staging.
"""

import re
from pathlib import Path
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from db.connection import (
    get_game_id, get_or_create_set, find_card_by_external_id,
    find_set_by_code, find_card_by_number_set,
    insert_card_master, insert_card_attributes
)
from db.staging import create_batch_id, insert_staging_row
from utils.pokemon_api import (
    search_cards, parse_card_master_fields, parse_card_attribute_fields
)
from utils.set_name_map import get_set_alias

ORDER_NUM_RE = re.compile(r'[A-F0-9]{8}-[A-F0-9]{6}-[A-F0-9]{5}')

CONDITION_MAP = {
    "near mint":                          "Near Mint",
    "near mint holofoil":                 "Near Mint",
    "near mint reverse holofoil":         "Near Mint",
    "lightly played":                     "Lightly Played",
    "lightly played holofoil":            "Lightly Played",
    "lightly played reverse holofoil":    "Lightly Played",
    "moderately played":                  "Moderately Played",
    "moderately played holofoil":         "Moderately Played",
    "moderately played reverse holofoil": "Moderately Played",
    "heavily played":                     "Heavily Played",
    "heavily played holofoil":            "Heavily Played",
    "heavily played reverse holofoil":    "Heavily Played",
    "damaged":                            "Damaged",
    "damaged holofoil":                   "Damaged",
    "damaged reverse holofoil":           "Damaged",
}

# Plain finish labels — replaced by foil_type, not kept as foil_pattern
FINISH_LABELS = {
    "holo",
    "holofoil",
    "non-holo",
    "normal",
}

# Special foil patterns — kept as foil_pattern
FOIL_PATTERNS = {
    "cosmos holo", "cosmos holo v",
    "cracked ice holo", "cracked ice",
    "master ball pattern", "master ball",
    "poke ball pattern", "pokeball pattern",
    "galaxy holo", "etched",
}


def _extract_foil_type(condition_raw: str):
    lower = condition_raw.lower()
    if "reverse holofoil" in lower:
        return "reverse holo"
    if "holofoil" in lower:
        return "holo"
    return None


def _extract_foil_fields(variant_raw, foil_type):
    """
    Split variant into (foil_type, foil_pattern).

    Examples:
        "Cosmos Holo" + "Holo"    → ("Holo", "Cosmos Holo")
        "Reverse Holo" + anything → ("Reverse Holo", None)
        "Master Ball Pattern"     → (foil_type, "Master Ball Pattern")
        None + "Holo"             → ("Holo", None)
        None + None               → (None, None)
    """
    if not variant_raw:
        return foil_type, None
    v_lower = variant_raw.lower()
    if v_lower in FOIL_PATTERNS:
        return foil_type, variant_raw
    if v_lower in ("reverse holo", "reverse holofoil"):
        return "reverse Holo", None
    if v_lower in FINISH_LABELS:
        return foil_type or "holo", None
    return foil_type, variant_raw


def _parse_items(text: str) -> list[dict]:
    """
    Parse card items from TCGPlayer order HTML text.

    Fixed 4-line structure before each price line:
        [i-4] Card Name (Variant) - number/total
        [i-3] Set Name
        [i-2] Rarity: ...
        [i-1] Condition: ...
        [i  ] $price
        [i+1] qty
    """
    items  = []
    lines  = [l.strip() for l in text.split("\n") if l.strip()]
    SUMMARY_LABELS = {"subtotal:", "shipping:", "sales tax", "total:",
                      "store credit:", "order total"}
    BAD_NAMES = {"order number", "order date", "channel", "items", "details",
                 "price", "quantity", "ship to", "bill to",
                 "shipped and sold by", "order summary", "rate transaction"}

    i = 0
    while i < len(lines):
        line = lines[i]

        if not re.match(r'^\$[\d]+\.[\d]{2}$', line):
            i += 1
            continue

        price = float(line.replace("$", ""))
        prev  = lines[i-1].lower() if i > 0 else ""

        if any(prev.startswith(s) for s in SUMMARY_LABELS):
            i += 1
            continue

        if i < 5:
            i += 1
            continue

        cond_line = lines[i-1]
        rar_line  = lines[i-2]
        set_line  = lines[i-3]

        if not cond_line.lower().startswith("condition:"):
            i += 1
            continue
        if not rar_line.lower().startswith("rarity:"):
            i += 1
            continue

        # Quantity
        qty = 1
        if i + 1 < len(lines):
            m = re.match(r'^(\d+)$', lines[i+1])
            if m and 1 <= int(m.group(1)) <= 999:
                qty = int(m.group(1))

        # Name line: i-4, but if it looks like a set name use i-5
        name_candidate = lines[i-4]
        is_set = bool(re.match(
            r'^(SV|SVE|SV\d+|ME|ME\d+|XY|BW|SWSH|SM|POP|DP|GS|RS|zsv|rsv)[:\s]',
            name_candidate, re.I
        ))
        if is_set and i >= 5:
            set_line  = name_candidate
            name_line = lines[i-5]
        else:
            name_line = name_candidate

        # Parse name: variant first, then number
        raw_line = name_line

        var_match = re.search(r'\s*\(([^)]+)\)\s*$', raw_line)
        if var_match:
            item_variant = var_match.group(1).strip()
            raw_line     = raw_line[:var_match.start()].strip()
        else:
            item_variant = None

        num_match = re.search(r'\s*-\s*([A-Za-z]{0,4}\d+[A-Za-z]?(?:/\d+[A-Za-z]*)?)[\s]*$', raw_line)
        if num_match:
            card_number = num_match.group(1).split("/")[0].strip()
            raw_line    = raw_line[:num_match.start()].strip()
        else:
            card_number = None

        card_name = raw_line.strip()

        # Parse condition and foil fields
        raw_cond              = cond_line.split(":", 1)[1].strip()
        condition             = CONDITION_MAP.get(raw_cond.lower(), "Near Mint")
        foil_type_raw         = _extract_foil_type(raw_cond)
        foil_type, foil_pattern = _extract_foil_fields(item_variant, foil_type_raw)

        # Skip bad names
        if not card_name or card_name.lower() in BAD_NAMES:
            i += 1
            continue
        if re.match(r'^(SV|SVE|SV\d+|ME|ME\d+|XY|BW|SWSH|SM)[:\s]', card_name, re.I):
            i += 1
            continue
        if card_name.lower().startswith("ion:"):
            i += 1
            continue
        if price <= 0:
            i += 1
            continue

        items.append({
            "card_name":    card_name,
            "card_number":  card_number,
            "foil_type":    foil_type,
            "foil_pattern": foil_pattern,
            "set_name":     set_line,
            "condition":    condition,
            "quantity":     qty,
            "price":        price,
        })

        i += 1
    return items


def _extract_order_totals(text: str) -> dict:
    """
    Pull Subtotal/Shipping/Sales Tax off the order summary block -- same
    "Label:" line immediately followed by a "$X.XX" line layout as the
    card price lines _parse_items reads. Any field not present (e.g. a
    tax-exempt order has no Sales Tax line at all) defaults to 0.0.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    totals = {"subtotal": 0.0, "shipping": 0.0, "tax": 0.0}
    for i, line in enumerate(lines[:-1]):
        m = re.match(r'^\$([\d,]+\.\d{2})$', lines[i + 1])
        if not m:
            continue
        low = line.lower()
        value = float(m.group(1).replace(",", ""))
        if low.startswith("subtotal:"):
            totals["subtotal"] = value
        elif low.startswith("shipping:"):
            totals["shipping"] = value
        elif low.startswith("sales tax"):
            totals["tax"] = value
    return totals


def _apply_shipping_and_tax(items: list[dict], order_text: str, verbose: bool):
    """
    Spreads this order's shipping + sales tax proportionally across its
    cards by value, so each card's cost_basis (staging.price flows
    straight into inventory.cost_basis for TCGPlayer rows -- see
    push_staging_row_to_inventory.sql) reflects its true landed cost
    instead of just the raw per-card price. Mutates items in place.
    """
    totals = _extract_order_totals(order_text)
    surcharge = totals["shipping"] + totals["tax"]
    if surcharge <= 0:
        return

    # Prefer the page's own Subtotal (what TCGPlayer actually computed the
    # surcharge against) over re-summing parsed items, in case a card was
    # missed/mis-parsed -- falls back to the item sum if Subtotal wasn't found.
    order_subtotal = totals["subtotal"] or sum(it["price"] * it["quantity"] for it in items)
    if order_subtotal <= 0:
        return

    rate = surcharge / order_subtotal
    _print(verbose, f"  Spreading shipping (${totals['shipping']:.2f}) + tax (${totals['tax']:.2f}) "
                     f"across {len(items)} card(s) proportionally ({rate:.1%} of card value)")
    for it in items:
        it["price"] = round(it["price"] * (1 + rate), 2)


def import_from_html(path: str, dry_run: bool = False,
                     verbose: bool = True,
                     only_order: str = None) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        print(f"Path not found: {path}")
        return {}

    html_files = (sorted(p.glob("*.html")) + sorted(p.glob("*.htm"))
                  if p.is_dir() else [p])
    if not html_files:
        print(f"No HTML files found in {path}")
        return {}

    batch_id = create_batch_id(prefix="TCGP")
    _print(verbose, f"\n=== TCGPlayer HTML Import ===")
    _print(verbose, f"Batch:  {batch_id}")
    _print(verbose, f"Files:  {len(html_files)}\n")

    totals  = {"staged": 0, "matched": 0, "ambiguous": 0, "not_found": 0}
    game_id = get_game_id("Pokemon")

    # Load import corrections once for all files
    from db.connection import get_import_corrections, apply_import_correction
    corrections = get_import_corrections()
    if corrections:
        _print(verbose, f"  Loaded {len(corrections)} import correction(s)")

    for html_file in html_files:
        _print(verbose, f"Processing {html_file.name}...")
        result = _process_file(html_file, batch_id, game_id, dry_run, verbose,
                               only_order=only_order, corrections=corrections)
        for k in totals:
            totals[k] += result[k]

    if not dry_run and totals["staged"] > 0:
        _print(verbose, "\nCalculating suggested prices...")
        try:
            from utils.pricing_engine import price_staging_batch
            price_staging_batch(batch_id)
        except Exception as e:
            _print(verbose, f"  Note: pricing skipped — {e}")

    _print(verbose, f"\n{'[DRY RUN] ' if dry_run else ''}Import complete.")
    _print(verbose, f"  Staged:    {totals['staged']} cards")
    _print(verbose, f"  Matched:   {totals['matched']} auto-resolved")
    _print(verbose, f"  Ambiguous: {totals['ambiguous']} need you to pick the right card")
    _print(verbose, f"  Not found: {totals['not_found']} not in PokemonTCG API")

    if not dry_run and totals["staged"] > 0:
        _print(verbose, f"\nNext steps:")
        _print(verbose, f"  python3 main.py --review   → review + fix staged cards")
        _print(verbose, f"  python3 main.py --approve  → push to inventory")

    return {**totals, "batch_id": batch_id}


def _process_file(html_file: Path, batch_id: str, game_id: str,
                  dry_run: bool, verbose: bool,
                  only_order: str = None, corrections: list = None) -> dict:
    with open(html_file, encoding="utf-8", errors="ignore") as f:
        content = f.read()

    soup = BeautifulSoup(content, "html.parser")
    text = soup.find("body").get_text(separator="\n", strip=True)

    order_numbers = list(dict.fromkeys(ORDER_NUM_RE.findall(text)))
    if not order_numbers:
        _print(verbose, "  No order numbers found in this file.")
        return {"staged": 0, "matched": 0, "ambiguous": 0, "not_found": 0}

    if only_order:
        order_numbers = [o for o in order_numbers if only_order in o]
        if not order_numbers:
            _print(verbose, f"  Order {only_order} not found in this file.")
            return {"staged": 0, "matched": 0, "ambiguous": 0, "not_found": 0}

    _print(verbose, f"  Orders: {', '.join(order_numbers)}")
    staged = matched = ambiguous = not_found = 0

    for order_num in order_numbers:
        # Card-level dedup: fetch all existing staging rows for this order
        existing_cards = {}  # key: (card_name, set_name, condition) -> status
        if not dry_run:
            from db.connection import db_cursor
            with db_cursor() as cur:
                cur.execute("""
                    SELECT card_name, set_name, condition, status
                    FROM staging
                    WHERE order_number = %s
                """, (order_num,))
                for r in cur.fetchall():
                    key = (r["card_name"], r["set_name"] or "", r["condition"])
                    # Keep the "best" status: processed > approved > pending
                    prev = existing_cards.get(key)
                    rank = {"processed": 2, "approved": 1, "pending": 0}
                    if prev is None or rank.get(r["status"], -1) > rank.get(prev, -1):
                        existing_cards[key] = r["status"]

        order_text = _extract_order_section(text, order_num)
        order_date = _extract_date(order_text)
        items      = _parse_items(order_text)
        _apply_shipping_and_tax(items, order_text, verbose)

        _print(verbose, f"\n  [{order_num}] {order_date.strftime('%Y-%m-%d')} — {len(items)} item(s)")

        for item in items:
            card_key = (item["card_name"], item.get("set_name", ""), item["condition"])

            if not dry_run and card_key in existing_cards:
                card_status = existing_cards[card_key]
                if card_status in ("approved", "processed"):
                    _print(verbose,
                        f"  ~ Qty:{item['quantity']:<3} Name:{item['card_name']:<28} "
                        f"SKIPPED — already {card_status}")
                    staged += 1
                    matched += 1
                    continue
                elif card_status == "pending":
                    # Delete stale pending row and reimport
                    with db_cursor() as cur:
                        cur.execute("""
                            DELETE FROM staging
                            WHERE order_number = %s
                              AND card_name = %s
                              AND COALESCE(set_name, '') = %s
                              AND condition = %s
                              AND status = 'pending'
                        """, (order_num, item["card_name"],
                              item.get("set_name", ""), item["condition"]))

            card_id, status, options = _resolve_card(item, game_id, dry_run)

            if not dry_run:
                insert_staging_row(
                    batch_id     = batch_id,
                    order_number = order_num,
                    order_date   = order_date,
                    card_name    = item["card_name"],
                    set_name     = item.get("set_name", ""),
                    condition    = item["condition"],
                    foil_type    = item.get("foil_type"),
                    foil_pattern = item.get("foil_pattern"),
                    quantity     = item["quantity"],
                    price        = item["price"],
                    card_id      = card_id,
                    match_status = status,
                    match_options= options,
                )
                # Store market price from API response if available
                api_market = (options[0].get("market_price")
                              if status == "matched" and options else None)
                if api_market and card_id:
                    try:
                        from db.connection import get_or_create_variant, upsert_market_price
                        foil_type    = item.get("foil_type")
                        foil_pattern = item.get("foil_pattern")
                        variant_type = foil_pattern or foil_type or "Non-Holo"
                        finish       = foil_type or "Non-Holo"
                        SPECIAL = {"Cosmos Holo","Master Ball Pattern",
                                   "Poke Ball Pattern","Cracked Ice Holo"}
                        v_id = get_or_create_variant(
                            card_id=card_id, variant_type=variant_type,
                            finish=finish, is_special=variant_type in SPECIAL
                        )
                        upsert_market_price(v_id, item["condition"],
                                            api_market, "tcgplayer")
                    except Exception:
                        pass

            staged += 1
            status_icon = "✓" if status == "matched" else ("?" if status == "ambiguous" else "✗")
            api_info    = options[0] if (status == "matched" and options) else {}
            api_num     = api_info.get("api_number", "—")
            api_set     = api_info.get("api_set", "—")
            api_rar     = api_info.get("api_rarity", "—")

            foil_str = item.get("foil_pattern") or item.get("foil_type") or "—"

            market_str = ""
            if status == "matched" and options:
                mp = options[0].get("market_price")
                market_str = f"Market:${mp:<7.2f} " if mp else "Market:—        "

            _print(verbose,
                f"  {status_icon} "
                f"Qty:{item['quantity']:<3} "
                f"Name:{item['card_name']:<28} "
                f"Foil:{foil_str:<22} "
                f"Cond:{item['condition']:<18} "
                f"Price:${item['price']:<7.2f} "
                f"TCG#:{str(item.get('card_number') or '—'):<6} "
                + (f"API#:{api_num:<6} {market_str}APISet:{api_set:<30} Rarity:{api_rar}"
                   if status == "matched"
                   else f"Set:{str(item.get('set_name','—'))}")
            )

            if status == "matched":
                matched += 1
            elif status == "ambiguous":
                ambiguous += 1
            else:
                not_found += 1

            # Auto-approve all matched rows for this order
            if not dry_run:
                with db_cursor() as cur:
                    cur.execute("""
                        UPDATE staging
                        SET status = 'approved', updated_at = NOW()
                        WHERE order_number = %s
                          AND match_status = 'matched'
                          AND status = 'pending'
                    """, (order_num,))

    return {"staged": staged, "matched": matched,
            "ambiguous": ambiguous, "not_found": not_found}


def _resolve_card(item: dict, game_id: str, dry_run: bool) -> tuple:
    # Step 0: check our own DB first, by (set, card number) -- the
    # reliable natural key within a set (same shortcut the Excel importer
    # uses via find_card_by_number_set). Skips the external API entirely
    # for a card we've already matched before, so re-importing known cards
    # isn't at the mercy of the PokemonTCG API's frequent flakiness/
    # outages. Resolved via tcgplayer_set_aliases (get_set_alias) --
    # TCGPlayer's raw label ("ME: Ascended Heroes") essentially never
    # matches card_sets.name exactly ("Ascended Heroes"), so a
    # find_set_by_name() lookup here would silently never hit. Falls
    # through to the live API for anything not found here (new cards,
    # unmapped sets, or a card_number format mismatch).
    card_number = item.get("card_number")
    set_name    = item.get("set_name")
    if card_number and set_name:
        alias = get_set_alias(set_name)
        set_id = None
        if alias:
            if alias.get("set_id"):
                set_id = str(alias["set_id"])
            elif alias.get("api_set_id"):
                existing_set = find_set_by_code(alias["api_set_id"])
                if existing_set:
                    set_id = str(existing_set["id"])
        if set_id:
            # Try both as-parsed and leading-zero-stripped -- TCGPlayer
            # zero-pads ("Charcadet - 022") but card_master often doesn't
            # ("22"), same normalization search_cards() already does for
            # the API path via numbers_to_try.
            numbers_to_try = [card_number]
            stripped = card_number.lstrip("0") or card_number
            if stripped != card_number:
                numbers_to_try.append(stripped)
            for num in numbers_to_try:
                local_matches = find_card_by_number_set(set_id, num)
                if len(local_matches) == 1:
                    row = local_matches[0]
                    return str(row["id"]), "matched", []

    # search_cards() -> _api_search() already retries transient failures
    # (5 attempts, 5s apart) before giving up; if it still raises after
    # that (API outage, connection error, etc.), don't let it take down
    # the rest of this order/file -- land the row in staging unmatched,
    # same as a genuine "not found", so it's just one more row to
    # manually match/review instead of losing the whole import.
    try:
        results = search_cards(
            name        = item["card_name"],
            set_name    = item.get("set_name"),
            card_number = item.get("card_number"),
            variant     = item.get("foil_pattern") or item.get("foil_type"),
        )
    except Exception as e:
        print(f"    x API error for {item['card_name']} #{item.get('card_number')} "
              f"({item.get('set_name')}) -- leaving unmatched for manual review: {e}")
        return None, "not_found", []

    if not results:
        return None, "not_found", []

    if len(results) > 1:
        options = [{"id": c["id"], "name": c["name"],
                    "card_number": c.get("number"),
                    "set": c["set"]["name"],
                    "variant": ", ".join(c.get("subtypes", []))}
                   for c in results]
        return None, "ambiguous", options

    api_card = results[0]

    # Extract market price from API response (no extra call needed)
    # Pick the right price key based on the card's foil type
    tcg_prices   = api_card.get("tcgplayer", {}).get("prices", {})
    market_price = None

    foil_type    = item.get("foil_type") if isinstance(item, dict) else None
    foil_pattern = item.get("foil_pattern") if isinstance(item, dict) else None

    # Determine best price key based on variant
    if foil_type == "reverse holo":
        price_key_order = ["reverseHolofoil", "holofoil", "normal"]
    elif foil_pattern in ("cosmos holo", "master ball pattern", "poke ball pattern"):
        price_key_order = ["holofoil", "reverseHolofoil", "normal"]
    else:
        price_key_order = ["holofoil", "normal", "reverseHolofoil", "1stEditionHolofoil"]

    for price_key in price_key_order:
        if price_key in tcg_prices and tcg_prices[price_key].get("market"):
            market_price = float(tcg_prices[price_key]["market"])
            break

    api_info = [{
        "api_name":    api_card["name"],
        "api_number":  api_card.get("number", "—"),
        "api_set":     api_card["set"]["name"],
        "api_rarity":  api_card.get("rarity", "—"),
        "market_price": market_price,
    }]

    existing = find_card_by_external_id(api_card["id"])
    if existing:
        return str(existing["id"]), "matched", api_info

    if dry_run:
        return "dry-run-id", "matched", api_info

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
    return card_id, "matched", api_info


def _extract_order_section(full_text: str, order_num: str) -> str:
    idx = full_text.find(order_num)
    if idx == -1:
        return full_text
    remaining  = full_text[idx + len(order_num):]
    next_match = ORDER_NUM_RE.search(remaining)
    end        = idx + len(order_num) + next_match.start() if next_match else len(full_text)
    return full_text[max(0, idx - 200):end]


def _extract_date(text: str) -> datetime:
    match = re.search(
        r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
        r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|'
        r'Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}', text
    )
    if match:
        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
            try:
                return datetime.strptime(match.group(0).strip(), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.now(timezone.utc)


def _print(verbose: bool, msg: str):
    if verbose:
        print(msg)
