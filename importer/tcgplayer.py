"""
importer/tcgplayer.py
Parses a TCGPlayer order export CSV and imports into the inventory database.

TCGPlayer CSV columns (typical):
  Order Number, Order Date, Quantity, Product, Set Name, Condition,
  Price, Seller, Tracking Number, ...

Usage:
  from importer.tcgplayer import import_tcgplayer_csv
  import_tcgplayer_csv("orders.csv", dry_run=False)
"""

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from db.connection import (
    get_game_id, get_or_create_set, find_card_by_external_id,
    find_card_by_name_set, insert_card_master, insert_card_attributes,
    insert_purchase, insert_inventory
)
from utils.pokemon_api import (
    search_cards, get_set_by_name, parse_card_master_fields,
    parse_card_attribute_fields
)

# ----------------------------------------------------------------
# Condition mapping: TCGPlayer → our standard
# ----------------------------------------------------------------
CONDITION_MAP = {
    "near mint":          "Near Mint",
    "nm":                 "Near Mint",
    "lightly played":     "Lightly Played",
    "lp":                 "Lightly Played",
    "moderately played":  "Moderately Played",
    "mp":                 "Moderately Played",
    "heavily played":     "Heavily Played",
    "hp":                 "Heavily Played",
    "damaged":            "Damaged",
    "d":                  "Damaged",
}

# ----------------------------------------------------------------
# Data structures
# ----------------------------------------------------------------

class ParsedRow(NamedTuple):
    order_number: str
    order_date:   datetime
    quantity:     int
    card_name:    str
    set_name:     str
    condition:    str          # normalized
    price_each:   float        # per-card price
    raw_condition: str         # original from CSV


class ImportResult(NamedTuple):
    total_rows:     int
    imported:       int
    skipped:        int
    flagged:        list        # rows needing manual review


# ----------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------

def import_tcgplayer_csv(filepath: str, dry_run: bool = False,
                         verbose: bool = True) -> ImportResult:
    """
    Parse a TCGPlayer order CSV and import into inventory.

    Args:
        filepath:  Path to the downloaded TCGPlayer CSV.
        dry_run:   If True, parses and validates but does not write to DB.
        verbose:   Print progress to stdout.

    Returns:
        ImportResult summary.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {filepath}")

    rows      = _parse_csv(path)
    flagged   = []
    imported  = 0
    skipped   = 0

    # Group rows by order number so we create one purchase per order
    orders: dict[str, list[ParsedRow]] = {}
    for row in rows:
        orders.setdefault(row.order_number, []).append(row)

    game_id = get_game_id("Pokemon")

    for order_num, order_rows in orders.items():
        _print(verbose, f"\nOrder {order_num} — {len(order_rows)} card(s)")

        total_cost  = sum(r.price_each * r.quantity for r in order_rows)
        card_count  = sum(r.quantity for r in order_rows)
        order_date  = order_rows[0].order_date

        purchase_id = None
        if not dry_run:
            purchase_id = insert_purchase(
                source        = "tcgplayer",
                purchase_type = "lot" if card_count > 1 else "single",
                reference_id  = order_num,
                total_cost    = total_cost,
                card_count    = card_count,
                purchased_at  = order_date,
            )

        for row in order_rows:
            result = _process_row(
                row, game_id, purchase_id, dry_run, verbose
            )
            if result == "imported":
                imported += 1
            elif result == "skipped":
                skipped += 1
            elif isinstance(result, dict):
                flagged.append(result)

    _print(verbose, f"\n{'[DRY RUN] ' if dry_run else ''}Done. "
           f"Imported: {imported} | Skipped: {skipped} | Flagged: {len(flagged)}")

    if flagged:
        _print(verbose, "\n--- Flagged for manual review ---")
        for i, f in enumerate(flagged, 1):
            _print(verbose, f"  {i}. {f['card_name']} / {f['set_name']} — {f['reason']}")
            for m in f.get("matches", []):
                _print(verbose, f"       → [{m['id']}] {m['name']} #{m.get('card_number')} ({m.get('variant')})")

    return ImportResult(
        total_rows = len(rows),
        imported   = imported,
        skipped    = skipped,
        flagged    = flagged,
    )


# ----------------------------------------------------------------
# Row processing
# ----------------------------------------------------------------

def _process_row(row: ParsedRow, game_id: str, purchase_id: str | None,
                 dry_run: bool, verbose: bool):
    """
    Resolve card_master entry and write inventory row.
    Returns 'imported', 'skipped', or a flagged dict.
    """
    card_id = _resolve_card(row, game_id, dry_run, verbose)

    if card_id is None:
        return "skipped"

    if isinstance(card_id, dict):
        # Ambiguous — needs manual review
        return card_id

    cost_basis = row.price_each

    _print(verbose, f"  ✓ {row.quantity}x {row.card_name} [{row.condition}] @ ${cost_basis:.2f}")

    if not dry_run:
        insert_inventory(
            card_id     = card_id,
            purchase_id = purchase_id,
            condition   = row.condition,
            quantity    = row.quantity,
            cost_basis  = cost_basis,
            acquired_at = row.order_date,
        )

    return "imported"


def _resolve_card(row: ParsedRow, game_id: str,
                  dry_run: bool, verbose: bool) -> str | dict | None:
    """
    Find or create a card_master entry for this row.
    Returns card_id string, a flagged dict (ambiguous), or None (skip).
    """
    # 1. Search PokemonTCG API
    api_results = search_cards(name=row.card_name, set_name=row.set_name)

    if not api_results:
        _print(verbose, f"  ✗ Not found in API: {row.card_name} / {row.set_name}")
        return {
            "card_name": row.card_name,
            "set_name":  row.set_name,
            "reason":    "No match found in PokemonTCG API",
            "matches":   [],
        }

    # 2. If multiple results, flag for review
    if len(api_results) > 1:
        matches = [
            {
                "id":          c["id"],
                "name":        c["name"],
                "card_number": c.get("number"),
                "variant":     _detect_variant_label(c),
            }
            for c in api_results
        ]
        _print(verbose, f"  ? Ambiguous: {len(api_results)} matches for {row.card_name} / {row.set_name}")
        return {
            "card_name": row.card_name,
            "set_name":  row.set_name,
            "reason":    f"{len(api_results)} possible matches — please pick one",
            "matches":   matches,
        }

    # 3. Single match — get or create card_master
    api_card = api_results[0]
    existing = find_card_by_external_id(api_card["id"])
    if existing:
        return str(existing["id"])

    # Parse fields and insert
    fields     = parse_card_master_fields(api_card)
    attr_fields = parse_card_attribute_fields(api_card)

    if dry_run:
        _print(verbose, f"  [dry run] Would create card_master: {fields['name']} #{fields['card_number']}")
        return "dry-run-placeholder"

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
# CSV parsing
# ----------------------------------------------------------------

def _parse_csv(path: Path) -> list[ParsedRow]:
    """Parse TCGPlayer CSV into a list of ParsedRow objects."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Normalize header keys
        for raw in reader:
            record = {k.strip().lower(): v.strip() for k, v in raw.items()}
            try:
                rows.append(ParsedRow(
                    order_number  = _get(record, "order number", "order #", "order_number"),
                    order_date    = _parse_date(_get(record, "order date", "date")),
                    quantity      = int(_get(record, "quantity", "qty") or 1),
                    card_name     = _get(record, "product", "card name", "name"),
                    set_name      = _get(record, "set name", "set", "expansion"),
                    condition     = _normalize_condition(_get(record, "condition")),
                    price_each    = float(_get(record, "price", "unit price", "item price").replace("$", "") or 0),
                    raw_condition = _get(record, "condition"),
                ))
            except Exception as e:
                print(f"  Warning: skipping malformed row — {e}", file=sys.stderr)
    return rows


def _get(record: dict, *keys: str) -> str:
    """Return first matching key value from record, or empty string."""
    for k in keys:
        if k in record and record[k]:
            return record[k]
    return ""


def _normalize_condition(raw: str) -> str:
    key = raw.strip().lower()
    return CONDITION_MAP.get(key, raw.strip())


def _parse_date(date_str: str) -> datetime:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _detect_variant_label(api_card: dict) -> str | None:
    subtypes = api_card.get("subtypes", [])
    if "Reverse Holo" in subtypes:
        return "Reverse Holo"
    if "1st Edition" in subtypes:
        return "1st Edition"
    return None


def _print(verbose: bool, msg: str):
    if verbose:
        print(msg)
