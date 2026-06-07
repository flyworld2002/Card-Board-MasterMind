"""
importer/ebay.py — Import eBay active listings into staging

Fetches all your active eBay variation listings via the Trading API
(GetMyeBaySelling + GetItem) and writes each variation as a row in
the staging table, ready for --review / --approve.

Usage:
    python main.py --ebay-import
    python main.py --ebay-import --dry-run
"""

import os
import re
import sys
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

from importer.ebay_auth import get_trading_headers, get_user_token, TRADING_API_URL
from utils.ebay_parser import parse_variation_name, infer_set_name_from_title

# ── Namespace used in eBay Trading API XML responses ─────────────────────────
NS = "urn:ebay:apis:eBLBaseComponents"


# ══════════════════════════════════════════════════════════════════════════════
# XML helpers
# ══════════════════════════════════════════════════════════════════════════════

def _find(node, tag: str):
    """Find first child element, handling namespace."""
    return node.find(f"{{{NS}}}{tag}")

def _findall(node, tag: str):
    return node.findall(f"{{{NS}}}{tag}")

def _text(node, tag: str, default=None):
    el = _find(node, tag)
    return el.text.strip() if el is not None and el.text else default

def _post(call_name: str, xml_body: str) -> ET.Element:
    """POST to Trading API and return parsed XML root."""
    resp = requests.post(
        TRADING_API_URL,
        headers=get_trading_headers(call_name),
        data=xml_body.encode("utf-8"),
        timeout=30,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    # Check for API-level errors
    ack = _text(root, "Ack", "")
    if ack not in ("Success", "Warning"):
        errors = _findall(root, "Errors")
        msgs = [_text(e, "LongMessage") or _text(e, "ShortMessage") for e in errors]
        raise RuntimeError(f"eBay API error ({call_name}): {'; '.join(filter(None, msgs))}")

    return root


# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — Get all active listing IDs via GetMyeBaySelling
# ══════════════════════════════════════════════════════════════════════════════

def fetch_active_listing_ids() -> list[dict]:
    """
    Returns list of dicts: [{item_id, title, quantity, price}, ...]
    Handles pagination automatically.
    """
    token    = get_user_token()
    all_items = []
    page      = 1

    print("📦 Fetching active eBay listings...")

    while True:
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  <ActiveList>
    <Include>true</Include>
    <Pagination>
      <EntriesPerPage>200</EntriesPerPage>
      <PageNumber>{page}</PageNumber>
    </Pagination>
  </ActiveList>
  <DetailLevel>ReturnAll</DetailLevel>
</GetMyeBaySellingRequest>"""

        root        = _post("GetMyeBaySelling", xml)
        active_list = _find(root, "ActiveList")

        if active_list is None:
            break

        items_array = _find(active_list, "ItemArray")
        if items_array is None:
            break

        items = _findall(items_array, "Item")
        if not items:
            break

        for item in items:
            item_id   = _text(item, "ItemID")
            title     = _text(item, "Title", "")
            qty       = _text(item, "Quantity", "1")

            # Price: prefer selling_status current price
            selling   = _find(item, "SellingStatus")
            price_str = None
            if selling is not None:
                cp = _find(selling, "CurrentPrice")
                if cp is not None:
                    price_str = cp.text

            if price_str is None:
                sp = _find(item, "StartPrice")
                price_str = sp.text if sp is not None else "0"

            all_items.append({
                "item_id":  item_id,
                "title":    title,
                "quantity": qty,
                "price":    price_str,
            })

        # Check if more pages
        pagination  = _find(active_list, "PaginationResult")
        total_pages = int(_text(pagination, "TotalNumberOfPages", "1"))
        print(f"  Page {page}/{total_pages} — {len(items)} listings")

        if page >= total_pages:
            break
        page += 1

    print(f"✅ Found {len(all_items)} active listings total.\n")
    return all_items


# ══════════════════════════════════════════════════════════════════════════════
#  Import Single Item
# ══════════════════════════════════════════════════════════════════════════════

def import_single_item(item_id: str, dry_run: bool = False, no_api: bool = False):
    """
    Import a single eBay listing by item ID.
    With --dry-run, calls the Pokemon TCG API to preview matches
    but writes nothing to the DB.
    With --no-api, skips API calls entirely — just shows parsed eBay data instantly.

    Usage:
        python3 main.py --ebay-item 334985403072
        python3 main.py --ebay-item 334985403072 --dry-run
        python3 main.py --ebay-item 334985403072 --dry-run --no-api
    """
    from utils.pokemon_api import lookup_card_for_ebay, extract_market_price

    batch_id = f"ebay_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"🔁 Single item import — item ID: {item_id}")
    if no_api:
        print("⚡ NO-API mode — showing parsed eBay data only, no API calls.\n")
    elif dry_run:
        print("⚠️  DRY RUN — API will be called but nothing written to DB.\n")

    print(f"  Fetching variations for item {item_id}...")
    try:
        rows = fetch_item_variations(item_id, f"eBay item {item_id}")
    except Exception as e:
        print(f"❌ Error fetching item {item_id}: {e}")
        return

    if not rows:
        print("No variations found for this item.")
        return

    print(f"  Found {len(rows)} variation(s).")

    # Real import — skip display loop, go straight to staging
    if not dry_run and not no_api:
        print(f"  Writing to staging...\n")
        inserted = write_to_staging(rows, batch_id, dry_run=False)
        print(f"\n✅ Done: {inserted} row(s) written to staging.")
        print(f"\nNext steps:")
        print(f"  python3 main.py --review      ← fix unmatched cards")
        print(f"  python3 main.py --approve-all ← push to inventory")
        return

    matched   = 0
    unmatched = 0

    for i, r in enumerate(rows, 1):
        card_name    = r.get("card_name") or r.get("variation_name", "")
        card_number  = r.get("card_number", "")
        set_name     = r.get("set_override") or r.get("set_name", "")
        variant_type = r.get("variant_type", "Normal")

        if r["quantity"] <= 0:
            print(f"  ⏭  [{i}/{len(rows)}] Skipped (qty=0): {card_name}\n")
            continue

        # ── No-API mode: just show parsed data instantly ──────────────────────
        if no_api:
            print(
                f"  📋 [{i}/{len(rows)}]\n"
                f"       eBay:      #{card_number} {card_name} | {variant_type} | qty={r['quantity']} | ${r['price']:.2f}\n"
                f"       Set:       {set_name}\n"
                f"       card_type: {r['card_type']}\n"
            )
            continue

        # ── API mode: look up card and get market price ───────────────────────
        lookup = lookup_card_for_ebay(
            card_name    = card_name,
            card_number  = card_number,
            set_name     = set_name,
            variant_type = variant_type,
        )

        if lookup["matched"]:
            matched += 1
            status = "✅"
        else:
            unmatched += 1
            status = "⚠️ "

        # Extract market price from cached api_card — no extra API call
        market_price, market_date = extract_market_price(lookup.get("_api_card"), variant_type)
        market_str   = f"${market_price:.2f}" if market_price else "—"

        price_diff = ""
        if market_price:
            diff       = r['price'] - market_price
            arrow      = "▲" if diff >= 0 else "▼"
            price_diff = f" ({arrow} ${abs(diff):.2f} vs market)"

        print(
            f"  {status} [{i}/{len(rows)}]\n"
            f"       eBay:      #{card_number} {card_name} | {variant_type} | qty={r['quantity']} | ${r['price']:.2f}{price_diff}\n"
            f"       API Match: {lookup['card_name'] or '—'} | "
            f"ID: {lookup['api_card_id'] or '—'} | "
            f"Set: {lookup['set_name'] or '—'} | "
            f"Rarity: {lookup['rarity'] or '—'}\n"
            f"       Market $:  {market_str}\n"
            f"       Image:     {lookup['image_url'] or '—'}\n"
            f"       card_type: {r['card_type']} | "
            f"card_id: {lookup['card_id'] or 'NOT CREATED (dry run)'}\n"
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"{'─'*60}")
    if no_api:
        print(f"📊 Total: {len(rows)} variation(s) parsed.")
        print(f"\n⚡ Run without --no-api to match against Pokemon TCG API.")
        return

    print(f"📊 Total: {len(rows)} | ✅ Matched: {matched} | ⚠️  Unmatched: {unmatched}")

    if dry_run:
        print(f"\n⚠️  DRY RUN complete — nothing written to DB.")
        print(f"    Run without --dry-run to import for real.")
        return

    # ── Real import — write to staging ────────────────────────────────────────
    print(f"\nWriting to staging...")
    inserted = write_to_staging(rows, batch_id, dry_run=False)
    print(f"\n✅ Done: {inserted} row(s) written to staging.")
    print(f"\nNext steps:")
    print(f"  python3 main.py --review      ← fix unmatched cards")
    print(f"  python3 main.py --approve-all ← push to inventory")

# ══════════════════════════════════════════════════════════════════════════════
# Step 2 — Get variation details for a single listing via GetItem
# ══════════════════════════════════════════════════════════════════════════════

def fetch_item_variations(item_id: str, title: str) -> list[dict]:
    """
    For a single listing ID, fetch all its variations.
    Returns list of dicts, one per variation:
    {
        item_id, title, variation_name,
        quantity, price,
        card_number, set_total, card_name,
        variant_type, card_type, set_name
    }
    """
    token = get_user_token()

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
  <IncludeItemSpecifics>true</IncludeItemSpecifics>
</GetItemRequest>"""

    root  = _post("GetItem", xml)
    item  = _find(root, "Item")
    if item is None:
        return []

    listing_title = _text(item, "Title", title)
    set_name      = infer_set_name_from_title(listing_title)

    variations_node = _find(item, "Variations")
    rows = []

    if variations_node is not None:
        # ── Variation listing ─────────────────────────────────────────────────
        for var in _findall(variations_node, "Variation"):
            # Quantity
            qty_str = _text(var, "Quantity", "0")

            # Price
            sp        = _find(var, "StartPrice")
            price_str = sp.text if sp is not None else "0.99"

            # Variation name is in VariationSpecifics → NameValueList → Value
            var_name = None
            specifics = _find(var, "VariationSpecifics")
            if specifics is not None:
                for nvl in _findall(specifics, "NameValueList"):
                    val_el = _find(nvl, "Value")
                    if val_el is not None:
                        var_name = val_el.text.strip()
                        break

            if not var_name:
                continue

            parsed = parse_variation_name(var_name, listing_title)

            rows.append({
                "item_id":        item_id,
                "title":          listing_title,
                "variation_name": var_name,
                "quantity":       int(qty_str),
                "price":          float(price_str),
                "set_name":       set_name or "",
                **{k: parsed[k] for k in (
                    "card_number", "set_total", "card_name",
                    "variant_type", "card_type", "parse_ok", "set_override",
                    "source_type"
                )},
            })

    else:
        # ── Single (non-variation) listing — treat the whole listing as one row
        qty_str   = _text(item, "Quantity", "1")
        sp        = _find(item, "StartPrice")
        price_str = sp.text if sp is not None else "0.99"

        parsed = parse_variation_name(listing_title, listing_title)
        rows.append({
            "item_id":        item_id,
            "title":          listing_title,
            "variation_name": listing_title,
            "quantity":       int(qty_str),
            "price":          float(price_str),
            "set_name":       set_name or "",
            **{k: parsed[k] for k in (
                "card_number", "set_total", "card_name",
                "variant_type", "card_type", "parse_ok"
            )},
        })

    return rows


# ══════════════════════════════════════════════════════════════════════════════
# Step 3 — Write rows to staging table
# ══════════════════════════════════════════════════════════════════════════════

def write_to_staging(rows: list[dict], batch_id: str, dry_run: bool = False) -> int:
    """
    Insert parsed variation rows into the staging table.
    Auto-matches each card via Pokemon TCG API and populates card_id.
    Skips rows where quantity == 0.
    Skips entire import if all variations already approved in staging.
    Only inserts missing variations if some are already there.
    Returns count of rows inserted.
    """
    from db.connection import db_cursor
    from utils.pokemon_api import lookup_card_for_ebay, rarity_to_card_type

    inserted  = 0
    skipped   = 0
    matched   = 0
    unmatched = 0

    # ── Check existing staging rows for this eBay item ────────────────────────
    if not dry_run:
        item_id = rows[0]["item_id"] if rows else None
        if item_id:
            with db_cursor() as cur:
                cur.execute("""
                    SELECT card_number, status
                    FROM staging
                    WHERE order_number = %s AND source = 'ebay'
                """, (item_id,))
                existing = cur.fetchall()

            existing_approved = set((r["card_number"], r.get("source_type") or "") for r in existing if r["status"] == "approved")
            existing_any      = set((r["card_number"], r.get("source_type") or "") for r in existing)
            incoming          = set((str(row.get("card_number", "")), row.get("source_type") or "") for row in rows if row["quantity"] > 0)

            # All variations already approved → skip entirely
            if incoming and incoming.issubset(existing_approved):
                print(f"  ✅ All {len(existing_approved)} variations already approved — skipping import.")
                return 0

            # Some already exist → only insert missing ones
            if existing_any:
                missing = incoming - existing_any
                if missing:
                    print(f"  ℹ️  Found {len(existing_any)} existing rows, inserting {len(missing)} missing variation(s).")
                    rows = [r for r in rows if (str(r.get("card_number", "")), r.get("source_type") or "") in missing]
                    for r in rows:
                        print(f"    Missing: #{r.get('card_number')} {r.get('card_name')} source={r.get('source_type')}")
                else:
                    print(f"  ✅ All variations already in staging — skipping import.")
                    return 0

    # ── Process rows ──────────────────────────────────────────────────────────
    for row in rows:
        if row["quantity"] <= 0:
            skipped += 1
            continue

        # ── Auto-match via Pokemon TCG API ────────────────────────────────────
        card_id      = None
        variant_id   = None
        match_status = "not_found"

        card_name    = row.get("card_name") or row.get("variation_name", "")
        card_number  = row.get("card_number", "")
        set_name     = row.get("set_override") or row.get("set_name", "")
        variant_type = row.get("variant_type", "Normal")

        if not dry_run and card_name and set_name:
            print(f"    🔍 Matching: {card_name} #{card_number} ({set_name})")
            lookup = lookup_card_for_ebay(
                card_name    = card_name,
                card_number  = card_number,
                set_name     = set_name,
                variant_type = variant_type,
            )
            if lookup["matched"]:
                card_id      = lookup["card_id"]
                variant_id   = lookup["variant_id"]
                match_status = "matched"
                matched += 1
                print(f"    ✅ Matched → {lookup['card_name']} [{lookup['source']}]")
                # Extract market price and date from cached api_card — no extra API call
                from utils.pokemon_api import extract_market_price
                market_price, market_date = extract_market_price(
                    lookup.get("_api_card"), variant_type
                )
                row["market_price"]      = market_price
                row["market_price_date"] = market_date
                row["api_rarity"]        = lookup.get("rarity")
                row["card_type"]         = rarity_to_card_type(lookup.get("rarity"))
            else:
                match_status = "not_found"
                unmatched += 1
                print(f"    ⚠️  No match found")

        if dry_run:
            print(
                f"  [DRY RUN] {row['set_name'] or '?':<22} "
                f"#{row['card_number'] or '?':<5} "
                f"{card_name:<28} "
                f"{variant_type:<16} "
                f"qty={row['quantity']}  ${row['price']:.2f}"
            )
            inserted += 1
            continue

        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO staging (
                    import_batch,
                    order_number,
                    order_date,
                    source,
                    card_name,
                    set_name,
                    card_number,
                    condition,
                    quantity,
                    price,
                    card_id,
                    match_status,
                    status,
                    notes,
                    market_price,
                    market_price_date,
                    api_rarity,
                    source_type   
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT DO NOTHING
            """, (
                batch_id,
                row["item_id"],
                datetime.now(timezone.utc),
                "ebay",
                card_name,
                set_name,
                row.get("card_number", ""),
                "Near Mint",
                row["quantity"],
                row["price"],
                card_id,
                match_status,
                "approved" if card_id else "pending",
                f"eBay: {row['title'][:80]} | var: {row['variation_name']} | type: {row['card_type']}",
                row.get("market_price"),
                row.get("market_price_date"),
                row.get("api_rarity"),
                row.get("source_type"),  
            ))
        inserted += 1

    if skipped:
        print(f"\n  ↳ Skipped {skipped} zero-quantity variation(s).")
    if not dry_run:
        print(f"\n  📊 Matched: {matched} | Needs review: {unmatched}")

    return inserted
    
# ══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════════════════════

def import_from_ebay(dry_run: bool = False):
    """
    Full eBay import flow:
    1. Fetch all active listing IDs
    2. For each listing, fetch variation details
    3. Parse each variation name into card fields
    4. Write to staging (or print if dry_run)
    """
    batch_id = f"ebay_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"🔁 eBay import batch: {batch_id}")
    if dry_run:
        print("⚠️  DRY RUN — nothing will be written to the database.\n")

    # ── 1. Get all active listing IDs ─────────────────────────────────────────
    listings = fetch_active_listing_ids()
    if not listings:
        print("No active listings found. Make sure your eBay token is valid.")
        return

    # ── 2 & 3. Fetch + parse variations for every listing ────────────────────
    all_rows     = []
    parse_errors = []

    for i, listing in enumerate(listings, 1):
        item_id = listing["item_id"]
        title   = listing["title"]
        print(f"  [{i}/{len(listings)}] Fetching: {title[:60]}")

        try:
            rows = fetch_item_variations(item_id, title)
            print(f"           → {len(rows)} variation(s)")
            all_rows.extend(rows)

            # Track any that failed to parse cleanly
            for r in rows:
                if not r.get("parse_ok"):
                    parse_errors.append(r["variation_name"])

        except Exception as e:
            print(f"  ⚠️  Error fetching item {item_id}: {e}")
            continue

    print(f"\n📊 Total variations parsed: {len(all_rows)}")

    if parse_errors:
        print(f"⚠️  {len(parse_errors)} variation(s) had parse issues (will still be staged for manual review):")
        for name in parse_errors[:10]:
            print(f"   • {name}")
        if len(parse_errors) > 10:
            print(f"   ... and {len(parse_errors) - 10} more")

    # ── 4. Write to staging ───────────────────────────────────────────────────
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Writing to staging...")
    inserted = write_to_staging(all_rows, batch_id, dry_run=dry_run)

    print(f"\n✅ Import complete: {inserted} row(s) {'would be ' if dry_run else ''}written to staging.")
    if not dry_run:
        print(f"\nNext steps:")
        print(f"  python main.py --review          ← review and fix conditions/prices")
        print(f"  python main.py --approve-all     ← push all approved rows to inventory")

  
# ══════════════════════════════════════════════════════════════════════════════
# Export listing to csv
# ══════════════════════════════════════════════════════════════════════════════

def export_listings_to_csv(no_api: bool = False, item_id: str = None):
    """
    Export all active eBay listings to a CSV file for review.
    
    NO DB WRITES — this is a read-only operation.
    
    Two modes:
    
    1. Default (with API):
       python3 main.py --ebay-export
       - Fetches all active eBay listings
       - Parses every variation name into card fields
       - Calls Pokemon TCG API to match each card
       - Exports full results including API match, rarity, market price
       - Use this to verify API matching before real import
    
    2. No API mode:
       python3 main.py --ebay-export --no-api
       - Fetches all active eBay listings
       - Parses every variation name into card fields
       - NO API calls — instant
       - Use this to check parsing and set name inference across all listings
       - Good first step to spot set name issues before API matching
    
    Output file: ebay_export_YYYYMMDD_HHMMSS.csv
    Location: same directory as main.py
    
    CSV columns (no-api mode):
        item_id, listing_title, variation_name, card_number, card_name,
        variant_type, card_type, set_name, set_override, qty, price
    
    CSV columns (api mode, adds):
        api_card_id, api_name, api_set, api_rarity, market_price,
        matched, match_source
    """
    import csv
    from datetime import datetime
    from utils.pokemon_api import lookup_card_for_ebay, extract_market_price

    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"ebay_export_{timestamp}.csv"

    mode_label = "no-api" if no_api else "with API matching"
    print(f"📦 eBay Export — {mode_label}")
    print(f"📄 Output file: {output_file}\n")

    # ── Step 1: Fetch all active listing IDs ─────────────────────────────────
    listings = fetch_active_listing_ids()
    if not listings:
        print("No active listings found.")
        return

    # Filter to single listing if item_id specified
    if item_id:
        listings = [l for l in listings if l["item_id"] == item_id]
        if not listings:
            print(f"❌ Item ID {item_id} not found in active listings.")
            return

    # ── Step 2: Fetch variations for every listing ───────────────────────────
    all_rows  = []
    for i, listing in enumerate(listings, 1):
        item_id = listing["item_id"]
        title   = listing["title"]
        print(f"  [{i}/{len(listings)}] Fetching: {title[:60]}")
        try:
            rows = fetch_item_variations(item_id, title)
            print(f"           → {len(rows)} variation(s)")
            all_rows.extend(rows)
        except Exception as e:
            print(f"  ⚠️  Error fetching item {item_id}: {e}")
            continue

    print(f"\n📊 Total variations fetched: {len(all_rows)}")

    # ── Step 3: Write CSV ─────────────────────────────────────────────────────
    # Define columns based on mode
    base_columns = [
        "item_id", "listing_title", "variation_name",
        "card_number", "card_name", "variant_type", "card_type",
        "set_name", "set_override", "qty", "price",
    ]
    api_columns = [
        "api_card_id", "api_name", "api_set", "api_rarity",
        "market_price", "matched", "match_source",
    ]
    columns = base_columns + ([] if no_api else api_columns)

    matched   = 0
    unmatched = 0

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()

        for i, row in enumerate(all_rows, 1):
            card_name    = row.get("card_name") or row.get("variation_name", "")
            card_number  = row.get("card_number", "")
            set_name     = row.get("set_override") or row.get("set_name", "")
            variant_type = row.get("variant_type", "Normal")

            # ── Base fields ───────────────────────────────────────────────────
            csv_row = {
                "item_id":        row["item_id"],
                "listing_title":  row["title"][:80],
                "variation_name": row["variation_name"],
                "card_number":    card_number,
                "card_name":      card_name,
                "variant_type":   variant_type,
                "card_type":      row.get("card_type", ""),
                "set_name":       row.get("set_name", ""),
                "set_override":   row.get("set_override", ""),
                "qty":            row["quantity"],
                "price":          row["price"],
            }

            # ── API fields ────────────────────────────────────────────────────
            if not no_api:
                if row["quantity"] <= 0:
                    csv_row.update({
                        "api_card_id":   "",
                        "api_name":      "",
                        "api_set":       "",
                        "api_rarity":    "",
                        "market_price":  "",
                        "matched":       "skipped (qty=0)",
                        "match_source":  "",
                    })
                else:
                    print(f"  [{i}/{len(all_rows)}] 🔍 {(set_name or '?'):<25} #{(card_number or '?'):<5} {card_name}")
                    lookup = lookup_card_for_ebay(
                        card_name    = card_name,
                        card_number  = card_number,
                        set_name     = set_name,
                        variant_type = variant_type,
                    )
                    market_price, _ = extract_market_price(
                        lookup.get("_api_card"), variant_type
                    )
                    if lookup["matched"]:
                        matched += 1
                    else:
                        unmatched += 1

                    csv_row.update({
                        "api_card_id":  lookup.get("api_card_id", ""),
                        "api_name":     lookup.get("card_name", ""),
                        "api_set":      lookup.get("set_name", ""),
                        "api_rarity":   lookup.get("rarity", ""),
                        "market_price": f"{market_price:.2f}" if market_price else "",
                        "matched":      "✅" if lookup["matched"] else "⚠️ not found",
                        "match_source": lookup.get("source", ""),
                    })

            writer.writerow(csv_row)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"✅ Export complete: {len(all_rows)} variations → {output_file}")
    if not no_api:
        print(f"📊 Matched: {matched} | Unmatched: {unmatched}")
    print(f"\nOpen the CSV in Excel or Google Sheets to review.")
    print(f"Fix any set name issues in utils/set_name_map.py before importing.")
