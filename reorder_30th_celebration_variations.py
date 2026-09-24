"""
reorder_30th_celebration_variations.py -- one-off: reorder the LIVE
VariationSpecificsSet value list on listing 336798749330 ("ME55 - 30th
Celebration Listing (Corrections)") to match
listing_card_assignments.priority_rank.

Drift cause: promoting a queued card on a display_sort='manual' listing
positioned it by raw card_number_numeric instead of priority_rank (fixed
in importer/ebay_listing_sync.py's _insert_position_sort_key() /
_compute_insert_position(), 2026-09-22) -- this script is the one-time
catch-up for cards already promoted wrong before that fix landed.

Run once: python3 reorder_30th_celebration_variations.py
"""

from db.connection import db_cursor
from importer.ebay import _find, _findall, _text
from importer.ebay_variations_xml import (
    fetch_item, deep_copy_variations, strip_selling_status,
    get_specifics_set, build_revise_xml, assert_no_picture_loss,
)
from importer.ebay import _post

ITEM_ID = "336798749330"
TEMPLATE_ID = "d571c58a-9b03-4b40-bc01-25c4e18fffc1"
ACCOUNT_NUM = 1


def main():
    item = fetch_item(ITEM_ID, account_num=ACCOUNT_NUM)
    original_variations = deep_copy_variations(item)  # untouched snapshot for assert_no_picture_loss()
    variations = deep_copy_variations(item)
    strip_selling_status(variations)

    specifics_set = get_specifics_set(variations)
    specific_name = next(iter(specifics_set), None)
    if not specific_name:
        raise SystemExit("No VariationSpecificsSet found on this listing.")

    existing_values = specifics_set[specific_name]
    print(f"Specific name: {specific_name!r}")
    print(f"Live value count: {len(existing_values)}")

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT elm.variation_name, lca.priority_rank
            FROM ebay_listing_map elm
            JOIN listing_card_assignments lca ON lca.variant_id = elm.variant_id
            WHERE elm.item_id = %s AND lca.template_id = %s
            """,
            (ITEM_ID, TEMPLATE_ID),
        )
        rank_by_name = {r["variation_name"]: r["priority_rank"] for r in cur.fetchall()}

    missing = [v for v in existing_values if v not in rank_by_name]
    if missing:
        print(f"WARNING: {len(missing)} live value(s) have no priority_rank match "
              f"(keeping original relative order, appended last): {missing}")

    def sort_key(v):
        return (0, rank_by_name[v]) if v in rank_by_name else (1, existing_values.index(v))

    new_order = sorted(existing_values, key=sort_key)

    if new_order == existing_values:
        print("Already in correct order -- nothing to do.")
        return

    print(f"\n{sum(1 for a, b in zip(existing_values, new_order) if a != b)} position(s) change:")
    for i, (old, new) in enumerate(zip(existing_values, new_order)):
        if old != new:
            print(f"  position {i}: {old!r}  ->  {new!r}")

    specifics_set_node = _find(variations, "VariationSpecificsSet")
    target_nvl = None
    for nvl in _findall(specifics_set_node, "NameValueList"):
        if _text(nvl, "Name") == specific_name:
            target_nvl = nvl
            break
    if target_nvl is None:
        raise SystemExit("Could not relocate NameValueList in the mutated tree.")

    value_elements = {v.text.strip(): v for v in _findall(target_nvl, "Value")}
    for v_el in _findall(target_nvl, "Value"):
        target_nvl.remove(v_el)
    for v in new_order:
        target_nvl.append(value_elements[v])

    assert_no_picture_loss(original_variations, variations)
    xml = build_revise_xml(ITEM_ID, variations, "ReviseFixedPriceItem", account_num=ACCOUNT_NUM)
    root = _post("ReviseFixedPriceItem", xml, account_num=ACCOUNT_NUM)
    ack = _text(root, "Ack")
    print(f"\nReviseFixedPriceItem Ack={ack}")
    if ack == "Warning":
        for e in _findall(root, "Errors"):
            print("  Warning:", _text(e, "LongMessage") or _text(e, "ShortMessage"))
    print("Done.")


if __name__ == "__main__":
    main()
