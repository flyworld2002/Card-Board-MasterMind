r"""
prune_stale_variation_specifics_sv1.py -- one-off: remove orphaned values from
listing 364198382892 ("Pokemon Scarlet and Violet Base Holo to Ultra Rare
Single Cards 1-198 YOU PICK", account 2 / BIGGYFISH2021)'s
VariationSpecificsSet that have no corresponding live <Variation> row.

Same root cause as prune_stale_variation_specifics.py (see its docstring):
insert_specifics_value() never removes a value from VariationSpecificsSet
once a card sells out/gets swapped, so the menu accumulates stale values
over the listing's lifetime -- 280 menu values vs 249 live <Variation> rows
here (31 orphans), found 2026-10-31 while trying to rename 5 typo'd live
variation names (ReviseItem rejected the rename with "Variation Specifics
provided does not match" until this menu/live mismatch is cleaned up first).

Does NOT touch the 249 live Variation rows or their prices/quantities --
only removes dead <Value> entries from the specifics menu, plus the
matching stale <VariationSpecificPictureSet> entries under <Pictures>
(eBay also rejects a revise once a picture entry's menu value is gone).
A precise check verifies no LIVE card's picture entry is touched before
posting.

Run once: python prune_stale_variation_specifics_sv1.py
"""

from importer.ebay import _find, _findall, _text, _post
from importer.ebay_variations_xml import (
    fetch_item, deep_copy_variations, strip_selling_status, normalize_quantities,
    get_specifics_set, get_variation_specifics, build_revise_xml,
)

ITEM_ID = "364198382892"
ACCOUNT_NUM = 2
EXPECTED_LIVE = 249


def main():
    item = fetch_item(ITEM_ID, account_num=ACCOUNT_NUM)
    original_variations = deep_copy_variations(item)  # untouched snapshot for the picture-loss check
    variations = deep_copy_variations(item)
    # MUST run before strip_selling_status (needs <QuantitySold>, which that
    # removes) -- without this, every untouched row's stale raw <Quantity>
    # gets eBay's sold-folding applied again on this revise, inflating
    # available stock by that row's sold count. This is exactly the bug
    # this script's first run hit (found + fixed 2026-10-31) -- see
    # fix_sv1_quantity_inflation.py and prepare_variations_for_revise()'s
    # docstring.
    normalize_quantities(variations)
    strip_selling_status(variations)

    specifics_set = get_specifics_set(variations)
    specific_name = next(iter(specifics_set), None)
    if not specific_name:
        raise SystemExit("No VariationSpecificsSet found on this listing.")
    menu_values = specifics_set[specific_name]
    print(f"Specific name: {specific_name!r}")
    print(f"Menu (VariationSpecificsSet) value count: {len(menu_values)}")

    live_vars = _findall(variations, "Variation")
    live_values = {
        get_variation_specifics(v).get(specific_name)
        for v in live_vars
    }
    live_values.discard(None)
    print(f"Live <Variation> count: {len(live_vars)}")
    print(f"Distinct live values: {len(live_values)}")

    orphans = [v for v in menu_values if v not in live_values]
    print(f"Orphaned menu values (no live Variation): {len(orphans)}")
    for v in orphans:
        print(f"  - {v!r}")

    if len(live_vars) != EXPECTED_LIVE:
        raise SystemExit(
            f"Live Variation count is {len(live_vars)}, expected {EXPECTED_LIVE} -- "
            f"aborting without writing, state doesn't match what was diagnosed."
        )
    if not orphans:
        print("Nothing to prune -- menu already matches live variations. Exiting.")
        return
    if len(menu_values) - len(orphans) != EXPECTED_LIVE:
        raise SystemExit(
            f"After removing {len(orphans)} orphans, menu would be "
            f"{len(menu_values) - len(orphans)}, expected {EXPECTED_LIVE} -- "
            f"aborting without writing, something doesn't add up."
        )

    specifics_set_node = _find(variations, "VariationSpecificsSet")
    target_nvl = None
    for nvl in _findall(specifics_set_node, "NameValueList"):
        if _text(nvl, "Name") == specific_name:
            target_nvl = nvl
            break
    if target_nvl is None:
        raise SystemExit("Could not relocate NameValueList in the mutated tree.")

    orphan_set = set(orphans)
    removed = 0
    for v_el in list(_findall(target_nvl, "Value")):
        if v_el.text and v_el.text.strip() in orphan_set:
            target_nvl.remove(v_el)
            removed += 1
    print(f"\nRemoved {removed} <Value> elements from the menu.")

    before_pics = _find(original_variations, "Pictures")
    before_live_pic_values = {
        _text(vsp, "VariationSpecificValue")
        for vsp in (_findall(before_pics, "VariationSpecificPictureSet") if before_pics is not None else [])
        if _text(vsp, "VariationSpecificValue") in live_values
    }

    pics = _find(variations, "Pictures")
    pics_removed = 0
    if pics is not None:
        for vsp in list(_findall(pics, "VariationSpecificPictureSet")):
            if _text(vsp, "VariationSpecificValue") in orphan_set:
                pics.remove(vsp)
                pics_removed += 1
    print(f"Removed {pics_removed} stale <VariationSpecificPictureSet> entries.")

    after_live_pic_values = {
        _text(vsp, "VariationSpecificValue")
        for vsp in (_findall(pics, "VariationSpecificPictureSet") if pics is not None else [])
        if _text(vsp, "VariationSpecificValue") in live_values
    }
    if after_live_pic_values != before_live_pic_values:
        raise SystemExit(
            f"Refusing to revise: a LIVE card's picture entry would be affected "
            f"by this prune (before={before_live_pic_values - after_live_pic_values}, "
            f"after-only={after_live_pic_values - before_live_pic_values}). "
            f"Aborting without writing."
        )

    xml = build_revise_xml(ITEM_ID, variations, "ReviseFixedPriceItem", account_num=ACCOUNT_NUM)
    root = _post("ReviseFixedPriceItem", xml, account_num=ACCOUNT_NUM)
    ack = _text(root, "Ack")
    print(f"\nReviseFixedPriceItem Ack={ack}")
    if ack == "Warning":
        for e in _findall(root, "Errors"):
            print("  Warning:", _text(e, "LongMessage") or _text(e, "ShortMessage"))
    if ack == "Failure":
        for e in _findall(root, "Errors"):
            print("  ERROR:", _text(e, "LongMessage") or _text(e, "ShortMessage"))
        raise SystemExit("Revise failed -- see errors above. Nothing further attempted.")

    # Verify
    item2 = fetch_item(ITEM_ID, account_num=ACCOUNT_NUM)
    variations2 = deep_copy_variations(item2)
    specifics_set2 = get_specifics_set(variations2)
    after_menu_count = len(specifics_set2.get(specific_name, []))
    after_live_count = len(_findall(variations2, "Variation"))
    print(f"\nAfter prune: menu value count = {after_menu_count}, live Variation count = {after_live_count}")


if __name__ == "__main__":
    main()
