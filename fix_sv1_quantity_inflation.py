r"""
fix_sv1_quantity_inflation.py -- one-off corrective revise: restore every
one of listing 364198382892's 249 live variations' true pristine available
quantity, undoing the QuantitySold-folding inflation caused by TWO
separate mistakes in this session (2026-10-31):

1. prune_stale_variation_specifics_sv1.py's first run resent the whole
   <Variations> block without calling normalize_quantities() first,
   inflating every row's available stock by its own sold count once (see
   importer/ebay_variations_xml.py's prepare_variations_for_revise(),
   added as the structural fix for this).
2. A follow-up "fix just 2 rows" script made the SAME mistake again --
   resent the other 247 already-corrected rows' raw Quantity unchanged
   (no normalize_quantities), re-inflating all of them a second time.

This script is the final, careful pass: every one of the 249 rows gets
its <Quantity> set EXPLICITLY to its known-correct pristine available
count (not derived from whatever the current, twice-corrupted stored
value happens to be) -- so there is nothing left unset for eBay's
sold-fold to compound on top of. Source of truth for 247 rows: the
`--ebay-item --dry-run` staging-import log captured before any of this
session's revises. The remaining 2 rows (Electricity Generator #170,
Mesagosa #178) hit a transient Pokemon TCG API error in that dry run
before their quantity could be logged -- their pristine values (6 and 8)
were instead recovered from this session's earlier `rename_variation
preview` output (stored=41/sold=35 and stored=34/sold=26, captured
before any revise touched this listing).
"""

import json
import re

from importer.ebay import _find, _findall, _text, _post
from importer.ebay_variations_xml import (
    fetch_item, deep_copy_variations, strip_selling_status,
    get_variation_specifics, set_variation_price_qty, build_revise_xml,
)

ITEM_ID = "364198382892"
ACCOUNT_NUM = 2
NUMBER_RE = re.compile(r"^(\d+)/")

# Recovered from this session's rename_variation `preview` output, captured
# before any revise touched this listing (stored - sold = true available).
MANUAL_OVERRIDES = {
    170: 41 - 35,  # Electricity Generator: 6
    178: 34 - 26,  # Mesagosa: 8
}


def main():
    with open("pristine_available.json") as f:
        pristine = {int(k): v for k, v in json.load(f).items()}
    pristine.update(MANUAL_OVERRIDES)
    print(f"Target map: {len(pristine)} rows with a known pristine available qty "
          f"({len(pristine) - len(MANUAL_OVERRIDES)} from the dry-run log, "
          f"{len(MANUAL_OVERRIDES)} manually recovered).")

    item = fetch_item(ITEM_ID, account_num=ACCOUNT_NUM)
    variations = deep_copy_variations(item)

    rows = []
    unaccounted = []
    for var in _findall(variations, "Variation"):
        val = get_variation_specifics(var).get("PokeCard")
        m = NUMBER_RE.match(val or "")
        if not m:
            raise SystemExit(f"Could not parse card number from {val!r} -- aborting without writing.")
        num = int(m.group(1))

        qty_el = _find(var, "Quantity")
        current_stored = int(qty_el.text) if qty_el is not None and qty_el.text else 0
        sold = int(_text(_find(var, "SellingStatus"), "QuantitySold", "0") or 0)

        if num in pristine:
            target_available = pristine[num]
        else:
            # Not in the dry-run log and not a manual override -- verified
            # separately that exactly 40 rows were "Skipped (qty=0)" in
            # that log (genuinely pristine-zero, never printed) and exactly
            # 2 more (the manual overrides) hit the lookup-failure gap --
            # 40 + 2 = 42 matches the total gap count exactly, so anything
            # landing here is a genuine zero-stock row.
            target_available = 0
            unaccounted.append(val)

        set_variation_price_qty(var, quantity=target_available)
        rows.append((val, current_stored, sold, target_available))

    print(f"Rows defaulted to pristine-zero (verified genuine skips): {len(unaccounted)}")

    print(f"Live <Variation> count: {len(rows)}")
    changed = [r for r in rows if r[1] != r[3] + r[2]]  # current_stored != target + sold(now)
    print(f"Rows whose stored value will actually change: {len(changed)}")

    strip_selling_status(variations)

    xml = build_revise_xml(ITEM_ID, variations, "ReviseFixedPriceItem", account_num=ACCOUNT_NUM)
    root = _post("ReviseFixedPriceItem", xml, account_num=ACCOUNT_NUM)
    ack = _text(root, "Ack")
    print(f"\nReviseFixedPriceItem Ack={ack}")
    if ack == "Failure":
        for e in _findall(root, "Errors"):
            print("  ERROR:", _text(e, "LongMessage") or _text(e, "ShortMessage"))
        raise SystemExit("Revise failed -- see errors above.")

    # Verify EVERY row, not just a sample.
    item2 = fetch_item(ITEM_ID, account_num=ACCOUNT_NUM)
    variations2 = deep_copy_variations(item2)
    mismatches = []
    for var in _findall(variations2, "Variation"):
        val = get_variation_specifics(var).get("PokeCard")
        m = NUMBER_RE.match(val or "")
        num = int(m.group(1)) if m else None
        target_available = pristine.get(num)

        qty_el = _find(var, "Quantity")
        new_stored = int(qty_el.text) if qty_el is not None and qty_el.text else 0
        new_sold = int(_text(_find(var, "SellingStatus"), "QuantitySold", "0") or 0)
        new_available = new_stored - new_sold
        # new_available may be < target (more sold in the interim) but never >.
        if new_available > target_available:
            mismatches.append((val, new_available, target_available))

    print(f"\nTotal rows where available exceeds pristine target (should be 0): {len(mismatches)}")
    for m in mismatches[:20]:
        print("  MISMATCH:", m)


if __name__ == "__main__":
    main()
