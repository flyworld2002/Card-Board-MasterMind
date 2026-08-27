"""
importer/tcgplayer_html.py
Imports TCGPlayer orders from saved HTML files into staging.
"""

import re
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup

from db.connection import (
    get_game_id, get_or_create_set, find_card_by_external_id,
    find_set_by_code, find_card_by_number_set, find_card_by_name_set,
    insert_card_master, insert_card_attributes
)
from db.staging import create_batch_id, insert_staging_row
from utils.pokemon_api import (
    search_cards, parse_card_master_fields, parse_card_attribute_fields
)
from utils.set_name_map import get_set_alias, _strip_tcgplayer_prefix

# Poke Ball / Master Ball pattern cards in these specific sets are genuine
# Reverse Holo prints, not straight Holo, regardless of what TCGPlayer's
# condition text says ("Holofoil", not "Reverse Holofoil") -- per Fei,
# 2026-08-27.
REVERSE_HOLO_PATTERN_SETS = {"Prismatic Evolutions", "Black Bolt", "White Flare"}
REVERSE_HOLO_OVERRIDE_PATTERNS = {"poke_ball", "master_ball"}

# "Cosmos Holo" belongs in the Texture axis (textures.code = "cosmos"),
# not Pattern -- foil_patterns has no matching code for it at all, so it
# was landing as unmatched free text. Per Fei, 2026-08-27.
COSMOS_TEXTURE_VARIANTS = {"cosmos holo", "cosmo holo", "cosmos holo v"}

ORDER_NUM_RE = re.compile(r'[A-F0-9]{8}-[A-F0-9]{6}-[A-F0-9]{5}')
API_WORKERS = 15  # matches excel_staging.py / market_price_refresh.py's precedent

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
    """Returns a foil_types.code value directly ("non_holo"/"holo"/
    "reverse_holo") -- Staging Review's Foil type dropdown selects by
    that exact DB code, not a display label."""
    lower = condition_raw.lower()
    if "reverse holofoil" in lower:
        return "reverse_holo"
    if "holofoil" in lower:
        return "holo"
    return "non_holo"


def _extract_foil_fields(variant_raw, foil_type):
    """
    Split variant into (foil_type, foil_pattern). foil_type is always a
    real foil_types.code ("non_holo"/"holo"/"reverse_holo") coming in via
    _extract_foil_type; foil_pattern is left as the RAW TCGPlayer text
    here -- _process_file resolves it to a real foil_patterns.code
    afterward (needs a DB lookup, which this pure parsing function
    doesn't have).

    Examples:
        "Cosmos Holo" + "holo"    → ("holo", "Cosmos Holo")
        "Reverse Holo" + anything → ("reverse_holo", None)
        "Master Ball Pattern"     → (foil_type, "Master Ball Pattern")
        None + "holo"             → ("holo", None)
        None + "non_holo"         → ("non_holo", None)
    """
    if not variant_raw:
        return foil_type, None
    v_lower = variant_raw.lower()
    if v_lower in FOIL_PATTERNS:
        return foil_type, variant_raw
    if v_lower in ("reverse holo", "reverse holofoil"):
        return "reverse_holo", None
    if v_lower in FINISH_LABELS:
        return foil_type if foil_type != "non_holo" else "holo", None
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
        rarity                = rar_line.split(":", 1)[1].strip()

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
            "rarity":       rarity,
            "raw_condition": raw_cond,
            "raw_variant":  item_variant,
        })

        i += 1
    return items


def _load_foil_pattern_codes() -> dict:
    """Maps foil_patterns.display_name.lower() -> code, loaded fresh from
    the DB each import (tiny table, cheap) -- a new pattern added to
    Staging Review's dropdown is picked up automatically, no code deploy
    needed, same reasoning as tcgplayer_set_aliases."""
    from db.connection import db_cursor
    with db_cursor() as cur:
        cur.execute("SELECT code, display_name FROM foil_patterns")
        return {r["display_name"].lower(): r["code"] for r in cur.fetchall()}


def _resolve_foil_pattern_code(raw_variant: str, pattern_codes: dict) -> str | None:
    """TCGPlayer's raw variant text ("Poke Ball Pattern", "Poke Ball",
    "Energy Symbol Pattern") -> the DB's real foil_patterns.code
    ("poke_ball", "energy_symbol"). Without this, Staging Review's
    Pattern dropdown shows the raw text as an unmatched custom value
    instead of selecting the actual option -- confirmed live, 2026-08-27:
    "Poke Ball Pattern" and "Energy Symbol Pattern" (TCGPlayer's own
    labels) don't match any foil_patterns row, whose display_name is
    just "Poke Ball" / "Energy Symbol", no "Pattern" suffix. Returns None
    (caller keeps the raw text) if nothing matches -- e.g. a genuinely
    new pattern not in the dropdown yet."""
    if not raw_variant:
        return None
    candidates = [raw_variant.strip().lower()]
    stripped = re.sub(r'\s*pattern\s*$', '', raw_variant, flags=re.I).strip()
    if stripped and stripped.lower() != candidates[0]:
        candidates.append(stripped.lower())
    for c in candidates:
        if c in pattern_codes:
            return pattern_codes[c]
    return None


def _build_source_notes(item: dict) -> str:
    """
    Renders exactly what TCGPlayer's page said for this line into a
    human-readable reference string, stored on the staging row's `notes`
    column -- so a manual match (ambiguous/not_found rows especially) has
    the raw source to check against instead of just our parsed/normalized
    fields, which can differ from the source (e.g. no card number printed
    at all, or a rarity that helps disambiguate multiple same-named cards).
    """
    parts = [f"TCGPlayer: {item['card_name']}"]
    if item.get("card_number"):
        parts.append(f"#{item['card_number']}")
    else:
        parts.append("(no # printed)")
    parts.append(f"| Set: {item.get('set_name') or '?'}")
    if item.get("rarity"):
        parts.append(f"| Rarity: {item['rarity']}")
    if item.get("raw_variant"):
        parts.append(f"| Variant: {item['raw_variant']}")
    parts.append(f"| Condition: {item.get('raw_condition') or item.get('condition') or '?'}")
    parts.append(f"| ${item['price']:.2f} x{item['quantity']}")
    return " ".join(parts)


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
                     only_order: str = None,
                     job_id: str = None) -> dict:
    """job_id is optional -- when run via job_runner.start_job() (the web
    upload path), progress is reported through update_job() so the Jobs
    page (and the Staging Review import modal) can show live status; a
    direct CLI call just omits it."""
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
                               only_order=only_order, corrections=corrections,
                               job_id=job_id)
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
                  only_order: str = None, corrections: list = None,
                  job_id: str = None) -> dict:
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

    pattern_codes = _load_foil_pattern_codes()

    # Parse every order's items up front so the total item count is known
    # before processing starts -- lets progress be reported as an actual
    # done/total (via update_job below) instead of just a spinner. Cheap:
    # parsing is pure string work, and a file normally has one order.
    parsed_orders = []
    for order_num in order_numbers:
        order_text = _extract_order_section(text, order_num)
        order_date = _extract_date(order_text)
        items      = _parse_items(order_text)
        # Snapshot the source-notes string BEFORE the surcharge distribution
        # below mutates item["price"] -- notes should reflect what
        # TCGPlayer's page actually said, not the tax/shipping-adjusted price.
        for it in items:
            it["_source_notes"] = _build_source_notes(it)
            if (it.get("foil_pattern") or "").strip().lower() in COSMOS_TEXTURE_VARIANTS:
                it["texture"] = "cosmos"
                it["foil_pattern"] = None
            code = _resolve_foil_pattern_code(it.get("foil_pattern"), pattern_codes)
            if code:
                it["foil_pattern"] = code
            cleaned_set = _strip_tcgplayer_prefix(it.get("set_name") or "")
            if cleaned_set in REVERSE_HOLO_PATTERN_SETS and it.get("foil_pattern") in REVERSE_HOLO_OVERRIDE_PATTERNS:
                it["foil_type"] = "reverse_holo"
        _apply_shipping_and_tax(items, order_text, verbose)
        parsed_orders.append((order_num, order_date, items))

    total_items = sum(len(items) for _, _, items in parsed_orders)
    done = 0
    if job_id:
        from importer.job_runner import update_job
        update_job(job_id, done=0, total=total_items,
                   staged=0, matched=0, ambiguous=0, not_found=0)

    def _report_progress():
        if job_id:
            from importer.job_runner import update_job
            update_job(job_id, done=done, total=total_items,
                       staged=staged, matched=matched,
                       ambiguous=ambiguous, not_found=not_found)

    for order_num, order_date, items in parsed_orders:
        # Card-level dedup: fetch all existing staging rows for this order.
        # Keyed on (card_name, condition) only -- NOT set_name. Confirmed
        # real bug (2026-08-26): once a row is manually matched/created
        # through the Staging Review UI, its set_name gets normalized to
        # the linked card's canonical card_sets.name (e.g. "Mega Evolution
        # Black Star Promos"), which no longer equals the raw label this
        # parser produces fresh on every re-import ("ME: Mega Evolution
        # Promo") -- keying on the 3-tuple silently missed the dedup match
        # and created duplicate staging rows for every manually-fixed card
        # on a re-import. card_name+condition is a weaker key (could in
        # theory merge two genuinely different same-named cards from
        # different sets bought in one order), but that's a rare edge case
        # and a safe failure mode (one gets skipped) versus the demonstrated
        # alternative (silent duplicate rows on every re-import).
        existing_cards = {}  # key: (card_name, condition) -> status
        if not dry_run:
            from db.connection import db_cursor
            with db_cursor() as cur:
                cur.execute("""
                    SELECT card_name, condition, status
                    FROM staging
                    WHERE order_number = %s
                """, (order_num,))
                for r in cur.fetchall():
                    key = (r["card_name"], r["condition"])
                    # Keep the "best" status: processed > approved > pending
                    prev = existing_cards.get(key)
                    rank = {"processed": 2, "approved": 1, "pending": 0}
                    if prev is None or rank.get(r["status"], -1) > rank.get(prev, -1):
                        existing_cards[key] = r["status"]

        _print(verbose, f"\n  [{order_num}] {order_date.strftime('%Y-%m-%d')} — {len(items)} item(s)")

        # ── Pass A: dedup-check + local-first resolution for every item in
        #    this order (fast, no network call) -- anything still
        #    unresolved after this gets queued for the parallel API pass.
        resolutions = {}   # idx -> ("skip", card_status) | (card_id, status, options)
        needs_api   = []   # indices needing the API

        for idx, item in enumerate(items):
            card_key = (item["card_name"], item["condition"])

            if not dry_run and card_key in existing_cards:
                card_status = existing_cards[card_key]
                if card_status in ("approved", "processed"):
                    resolutions[idx] = ("skip", card_status)
                    continue
                elif card_status == "pending":
                    # Delete stale pending row and reimport
                    with db_cursor() as cur:
                        cur.execute("""
                            DELETE FROM staging
                            WHERE order_number = %s
                              AND card_name = %s
                              AND condition = %s
                              AND status = 'pending'
                        """, (order_num, item["card_name"], item["condition"]))

            local, api_fallback_ok = _resolve_local(item)
            if local:
                resolutions[idx] = local
            elif not api_fallback_ok:
                # Set is only known locally (no api_set_id) -- an
                # unfiltered API search here would risk matching an
                # unrelated card in some other set. Land unmatched
                # instead of guessing wrong.
                _print(verbose, f"    x {item['card_name']} #{item.get('card_number') or '—'} "
                                f"({item.get('set_name')}) -- no local match and this set has no "
                                f"PokemonTCG API entry to search safely -- leaving unmatched for manual review")
                resolutions[idx] = (None, "not_found", [])
            else:
                needs_api.append(idx)

        # ── Pass B: PokemonTCG API fallback for everything not resolved
        #    locally, fired in parallel -- each is an independent HTTP call
        #    (same ThreadPoolExecutor pattern as importer/excel_staging.py
        #    and importer/market_price_refresh.py). Reports its own
        #    api_done/api_total via update_job separately from done/total
        #    (which only advances once a card is actually written in Pass
        #    C) -- otherwise this phase looks completely frozen from the
        #    outside for however long it takes, even though work is
        #    actively happening across all workers.
        if needs_api:
            _print(verbose, f"  Checking PokemonTCG API for {len(needs_api)} "
                            f"card(s) not found locally (parallel, {API_WORKERS} workers)...")
            if job_id:
                from importer.job_runner import update_job
                update_job(job_id, api_done=0, api_total=len(needs_api))
            api_raw = {}
            api_done = 0
            with ThreadPoolExecutor(max_workers=API_WORKERS) as pool:
                futures = {pool.submit(_search_api, items[i]): i for i in needs_api}
                for f in as_completed(futures):
                    idx = futures[f]
                    api_raw[idx] = f.result()
                    api_done += 1
                    if job_id:
                        from importer.job_runner import update_job
                        update_job(job_id, api_done=api_done, api_total=len(needs_api))
            for idx in needs_api:
                results, error = api_raw[idx]
                if error:
                    item = items[idx]
                    _print(verbose, f"    x API error for {item['card_name']} #{item.get('card_number')} "
                                    f"({item.get('set_name')}) -- leaving unmatched for manual review: {error}")
                    resolutions[idx] = (None, "not_found", [])
                else:
                    resolutions[idx] = _finalize_api_results(items[idx], results, game_id, dry_run)

        # ── Pass C: write to staging, in original item order (sequential --
        #    avoids concurrent-insert races on shared resources).
        for idx, item in enumerate(items):
            resolution = resolutions[idx]

            if resolution[0] == "skip":
                card_status = resolution[1]
                _print(verbose,
                    f"  ~ Qty:{item['quantity']:<3} Name:{item['card_name']:<28} "
                    f"SKIPPED — already {card_status}")
                staged += 1
                matched += 1
                done += 1
                _report_progress()
                continue

            card_id, status, options = resolution

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
                    texture      = item.get("texture"),
                    quantity     = item["quantity"],
                    price        = item["price"],
                    card_id      = card_id,
                    match_status = status,
                    match_options= options,
                    notes        = item["_source_notes"],
                    # Strip leading zeros ("093" -> "93") -- card_master
                    # stores promo/secret-rare numbers without padding, so
                    # the raw TCGPlayer-printed form otherwise triggers a
                    # false "number mismatch" warning in Staging Review
                    # even when the match is exactly correct.
                    card_number  = (item.get("card_number") or "").lstrip("0") or item.get("card_number"),
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

            done += 1
            _report_progress()

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


def _resolve_local(item: dict) -> tuple:
    """Step 0: check our own DB first, by (set, card number) -- the
    reliable natural key within a set (same shortcut the Excel importer
    uses via find_card_by_number_set). Skips the external API entirely
    for a card we've already matched before, so re-importing known cards
    isn't at the mercy of the PokemonTCG API's frequent flakiness/
    outages. Resolved via tcgplayer_set_aliases (get_set_alias) --
    TCGPlayer's raw label ("ME: Ascended Heroes") essentially never
    matches card_sets.name exactly ("Ascended Heroes"), so a
    find_set_by_name() lookup here would silently never hit.

    Returns (resolution, api_fallback_ok):
      resolution: (card_id, "matched", []) on a confident match (by
        number, or by name alone within a known set if the number didn't
        match/wasn't printed at all), else None.
      api_fallback_ok: False when this item's set is only known LOCALLY
        (alias.set_id, no api_set_id) -- search_cards() would then run
        with NO set filter at all, risking a match against an unrelated
        card that happens to share the same name+number in a totally
        different set. Confirmed real: an unrelated "Drifblim" print
        from "Supreme Victors" matched over the correct Mega Evolution
        Promo one once its printed number ("006") didn't match
        card_master's real number ("3", corrected 2026-08-26). Caller
        should land the card as not_found instead of trying the API in
        that case, rather than risk a wrong match.
    """
    card_number = item.get("card_number")
    set_name    = item.get("set_name")
    if not set_name:
        return None, True

    alias = get_set_alias(set_name)
    set_id = None
    api_fallback_ok = True
    if alias:
        if alias.get("set_id"):
            set_id = str(alias["set_id"])
            if not alias.get("api_set_id"):
                api_fallback_ok = False
        elif alias.get("api_set_id"):
            existing_set = find_set_by_code(alias["api_set_id"])
            if existing_set:
                set_id = str(existing_set["id"])

    if not set_id:
        return None, api_fallback_ok

    if card_number:
        # Try both as-parsed and leading-zero-stripped -- TCGPlayer
        # zero-pads ("Charcadet - 022") but card_master often doesn't
        # ("22"), same normalization search_cards() already does for
        # the API path.
        numbers_to_try = [card_number]
        stripped = card_number.lstrip("0") or card_number
        if stripped != card_number:
            numbers_to_try.append(stripped)
        for num in numbers_to_try:
            local_matches = find_card_by_number_set(set_id, num)
            if len(local_matches) == 1:
                row = local_matches[0]
                return (str(row["id"]), "matched", []), api_fallback_ok

    # No number printed, or it didn't match anything -- but the set IS
    # known for certain, so try name-only within just that set before
    # giving up. Only trusted when it's an unambiguous single match
    # (a popular name can have multiple prints within one set).
    name_matches = find_card_by_name_set(item["card_name"], set_id)
    if len(name_matches) == 1:
        return (str(name_matches[0]["id"]), "matched", []), api_fallback_ok

    return None, api_fallback_ok


def _search_api(item: dict) -> tuple:
    """Wraps search_cards() for use inside a ThreadPoolExecutor worker --
    returns (results, None) on success or (None, error_message) on
    failure, never raises. search_cards() -> _api_search() already
    retries transient failures (5 attempts, 5s apart) before giving up;
    if it still raises after that (API outage, connection error, etc.),
    this must not propagate -- a raised exception inside a worker thread
    would otherwise surface via future.result() and take down the whole
    parallel batch (and the rest of the import) over one bad card."""
    try:
        results = search_cards(
            name        = item["card_name"],
            set_name    = item.get("set_name"),
            card_number = item.get("card_number"),
            variant     = item.get("foil_pattern") or item.get("foil_type"),
        )
        return results, None
    except Exception as e:
        return None, str(e)


def _finalize_api_results(item: dict, results: list, game_id: str, dry_run: bool) -> tuple:
    """Turns already-fetched search_cards() results into a
    (card_id, status, options) resolution -- ambiguous/not_found/reuse-or-
    create card_master. Decoupled from making the API call itself so the
    call can run in parallel (see _search_api) while this part -- which
    writes to card_master/card_sets -- stays sequential, avoiding a
    concurrent-insert race on get_or_create_set/insert_card_master (same
    reasoning as importer/excel_staging.py's _finalize_api_match)."""
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


def _resolve_card(item: dict, game_id: str, dry_run: bool) -> tuple:
    """Convenience wrapper composing _resolve_local + _search_api +
    _finalize_api_results sequentially for a single card. _process_file's
    real import path uses the split functions directly (parallel API
    lookups across a whole order's unresolved cards); this wrapper exists
    for simple/CLI-style single-card resolution and ad-hoc testing."""
    local, api_fallback_ok = _resolve_local(item)
    if local:
        return local
    if not api_fallback_ok:
        return None, "not_found", []

    results, error = _search_api(item)
    if error:
        print(f"    x API error for {item['card_name']} #{item.get('card_number')} "
              f"({item.get('set_name')}) -- leaving unmatched for manual review: {error}")
        return None, "not_found", []

    return _finalize_api_results(item, results, game_id, dry_run)


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
