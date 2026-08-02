"""
importer/excel_staging.py
Imports a filled-out spreadsheet (see docs/plans/card_import_template.xlsx
for the expected columns) into staging.

Auto-matches each row to an existing card_master row by set + printed card
number. When nothing matches, creates a new card_master row directly from
the row's own columns — no PokemonTCG API call — since this importer exists
specifically for cards that may not be in that API at all. A row that
matches more than one existing card is left "ambiguous" for --review to
resolve by hand.
"""

from datetime import datetime, timezone
from pathlib import Path

from openpyxl import load_workbook

from db.connection import (
    get_game_id, find_set_by_name, get_or_create_set,
    find_card_by_number_set, insert_card_master
)
from db.staging import create_batch_id, insert_staging_row, update_staging_row

REQUIRED_COLUMNS = ["card_name", "set_name", "card_number",
                    "condition", "quantity", "price"]
VALID_CONDITIONS = {"Near Mint", "Lightly Played", "Moderately Played",
                    "Heavily Played", "Damaged"}
AXIS_COLUMNS = ["foil_type", "foil_pattern", "texture", "material",
                "size", "stamp_type", "source_type"]
TRUE_STRINGS = {"true", "yes", "y", "1"}


def import_from_excel(path: str, dry_run: bool = False, verbose: bool = True) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        print(f"File not found: {path}")
        return {}

    wb = load_workbook(p, data_only=True)
    ws = wb["Cards"] if "Cards" in wb.sheetnames else wb.worksheets[0]

    header_row = next(ws.iter_rows(min_row=1, max_row=1))
    headers = [(c.value or "").strip() if isinstance(c.value, str) else c.value
               for c in header_row]
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing_cols:
        print(f"Missing required column(s) in '{ws.title}': {', '.join(missing_cols)}")
        return {}

    batch_id = create_batch_id()
    _print(verbose, "\n=== Excel Import ===")
    _print(verbose, f"Batch: {batch_id}")
    _print(verbose, f"File:  {p.name}\n")

    game_id = get_game_id("Pokemon")
    totals = {"staged": 0, "matched": 0, "ambiguous": 0, "skipped": 0, "created": 0}

    for row_idx, cells in enumerate(ws.iter_rows(min_row=2), start=2):
        row = dict(zip(headers, [c.value for c in cells]))
        if not row.get("card_name"):
            continue  # blank row (e.g. trailing rows in the sheet)

        missing = [c for c in REQUIRED_COLUMNS if row.get(c) in (None, "")]
        if missing:
            _print(verbose, f"  x Row {row_idx}: missing {', '.join(missing)} — skipped")
            totals["skipped"] += 1
            continue

        condition = str(row["condition"]).strip()
        if condition not in VALID_CONDITIONS:
            _print(verbose, f"  x Row {row_idx}: unrecognized condition "
                            f"'{condition}' — skipped")
            totals["skipped"] += 1
            continue

        try:
            quantity = int(row["quantity"])
            price = float(row["price"])
        except (TypeError, ValueError):
            _print(verbose, f"  x Row {row_idx}: quantity/price not numeric — skipped")
            totals["skipped"] += 1
            continue

        card_id, match_status, options, created = _resolve_or_create_card(
            row, game_id, dry_run
        )

        if card_id is None and match_status == "not_found":
            _print(verbose, f"  x Row {row_idx}: {options[0]['reason']} — skipped")
            totals["skipped"] += 1
            continue

        purchase_date = _parse_date(row.get("purchase_date"))
        order_number = str(row.get("reference_id") or "").strip() or None
        source = str(row.get("source") or "other").strip()

        if not dry_run:
            staging_id = insert_staging_row(
                batch_id=batch_id,
                order_number=order_number,
                order_date=purchase_date,
                card_name=str(row["card_name"]).strip(),
                set_name=str(row["set_name"]).strip(),
                condition=condition,
                quantity=quantity,
                price=price,
                source=source,
                card_id=card_id,
                match_status=match_status,
                match_options=options,
                foil_type=row.get("foil_type"),
                foil_pattern=row.get("foil_pattern"),
                texture=row.get("texture"),
                material=row.get("material"),
                size=row.get("size"),
                stamp_type=row.get("stamp_type"),
                source_type=row.get("source_type"),
                is_shiny=_to_bool(row.get("is_shiny")),
            )
            if match_status == "matched":
                update_staging_row(staging_id, status="approved")

        totals["staged"] += 1
        if match_status == "matched":
            totals["matched"] += 1
            if created:
                totals["created"] += 1
        elif match_status == "ambiguous":
            totals["ambiguous"] += 1

        icon = "+" if created else ("v" if match_status == "matched" else "?")
        note = " (new card created)" if created else ""
        _print(verbose,
               f"  {icon} Row {row_idx}: {quantity}x {row['card_name']} "
               f"#{row['card_number']} [{condition}] ${price:.2f}{note}")

    _print(verbose, f"\n{'[DRY RUN] ' if dry_run else ''}Import complete.")
    _print(verbose, f"  Staged:    {totals['staged']} row(s)")
    _print(verbose, f"  Matched:   {totals['matched']} ({totals['created']} newly created)")
    _print(verbose, f"  Ambiguous: {totals['ambiguous']} need you to pick the right card")
    _print(verbose, f"  Skipped:   {totals['skipped']} (bad/incomplete rows)")

    if not dry_run and totals["staged"] > 0:
        _print(verbose, "\nNext steps:")
        if totals["ambiguous"]:
            _print(verbose, "  python3 main.py --review   -> resolve ambiguous matches")
        _print(verbose, "  python3 main.py --approve  -> push approved rows to inventory")

    return {**totals, "batch_id": batch_id}


def _resolve_or_create_card(row: dict, game_id: str, dry_run: bool):
    """
    Returns (card_id, match_status, match_options, created_new).
    match_status: 'matched' | 'ambiguous' | 'not_found'
    """
    set_name = str(row["set_name"]).strip()
    set_code = str(row.get("set_code") or "").strip() or None
    card_name = str(row["card_name"]).strip()
    card_number = str(row["card_number"]).strip()

    existing_set = find_set_by_name(set_name)
    if existing_set:
        set_id = str(existing_set["id"])
    elif set_code:
        if dry_run:
            return "dry-run-id", "matched", None, True
        set_id = get_or_create_set(game_id=game_id, name=set_name, set_code=set_code)
    else:
        return None, "not_found", [{
            "reason": f"Unknown set '{set_name}' — set_code required to create it"
        }], False

    candidates = find_card_by_number_set(set_id, card_number)

    if len(candidates) == 1:
        return str(candidates[0]["id"]), "matched", None, False

    if len(candidates) > 1:
        options = [{"card_id": str(c["id"]), "name": c["name"],
                    "card_number": c["card_number"], "rarity": c.get("rarity")}
                   for c in candidates]
        return None, "ambiguous", options, False

    # No existing card — create directly from the spreadsheet's own data,
    # no PokemonTCG API call (this importer is meant to also cover cards
    # that aren't in that API at all).
    if dry_run:
        return "dry-run-id", "matched", None, True

    card_id = insert_card_master(
        set_id=set_id,
        name=card_name,
        card_number=card_number,
        rarity=(str(row["rarity"]).strip() if row.get("rarity") else None),
        is_promo=_to_bool(row.get("is_promo")),
        is_first_edition=_to_bool(row.get("is_first_edition")),
        image_url=(str(row["image_url"]).strip() if row.get("image_url") else None),
    )
    return card_id, "matched", None, True


def _to_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in TRUE_STRINGS


def _parse_date(val):
    if val is None or val == "":
        return datetime.now(timezone.utc)
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(str(val).strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _print(verbose: bool, msg: str):
    if verbose:
        print(msg)
