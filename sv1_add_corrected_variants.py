r"""
sv1_add_corrected_variants.py -- one-off, step 2 of 2: add the 5
correctly-named <Variation> rows to listing 364198382892, after
sv1_remove_typo_variants.py already removed the typo'd rows.

Uses qty/price/picture captured in sv1_old_variant_data.json BEFORE the
removal (the old rows no longer exist live to copy from). Pictures reuse
the SAME already eBay-hosted URL the old row had -- eBay's own CDN still
serves it, no re-upload needed.

Also prunes the now-orphaned menu + picture-set entries for the 5 OLD
(removed) values in the same pass, rather than leaving them for a future
prune_stale_variation_specifics_sv1.py run.
"""

import json

from importer.ebay import _find, _findall, _text, _post
from importer.ebay_variations_xml import (
    fetch_item, deep_copy_variations, normalize_quantities, strip_selling_status,
    get_specifics_set, insert_specifics_value, add_variation_row, set_variation_picture,
    get_variation_specifics, build_revise_xml,
)

ITEM_ID = "364198382892"
ACCOUNT_NUM = 2

OLD_TO_NEW = {
    "029/198 Scovillian Reverse Holo RH": "029/198 Scovillain Reverse Holo RH",
    "169/198 Defiant Band Reverse Holo RH": "169/198 Defiance Band Reverse Holo RH",
    "170/198 Electricity Generator Reverse Holo RH": "170/198 Electric Generator Reverse Holo RH",
    "178/198 Mesagosa Reverse Holo RH": "178/198 Mesagoza Reverse Holo RH",
    "214/198 Grevard": "214/198 Greavard",
}


def main():
    with open("sv1_old_variant_data.json") as f:
        old_data = json.load(f)

    item = fetch_item(ITEM_ID, account_num=ACCOUNT_NUM)
    variations = deep_copy_variations(item)
    normalize_quantities(variations)
    strip_selling_status(variations)

    specifics_set = get_specifics_set(variations)
    specific_name = next(iter(specifics_set), None)
    if not specific_name:
        raise SystemExit("No VariationSpecificsSet found on this listing.")

    # Prune the now-orphaned OLD menu values + their picture entries
    # (removal left them dangling with prune_menu=False).
    specifics_set_node = _find(variations, "VariationSpecificsSet")
    pruned_menu = 0
    for nvl in _findall(specifics_set_node, "NameValueList"):
        if _text(nvl, "Name") != specific_name:
            continue
        for v_el in list(_findall(nvl, "Value")):
            if v_el.text and v_el.text.strip() in OLD_TO_NEW:
                nvl.remove(v_el)
                pruned_menu += 1

    pics = _find(variations, "Pictures")
    pruned_pics = 0
    if pics is not None:
        for vsp in list(_findall(pics, "VariationSpecificPictureSet")):
            if _text(vsp, "VariationSpecificValue") in OLD_TO_NEW:
                pics.remove(vsp)
                pruned_pics += 1

    print(f"Pruned {pruned_menu} orphaned menu value(s), {pruned_pics} orphaned picture entry(ies).")

    # Add each new value to the menu, then its <Variation> row.
    for old_value, new_value in OLD_TO_NEW.items():
        insert_specifics_value(variations, specific_name, new_value)
        data = old_data[old_value]
        add_variation_row(
            variations,
            {specific_name: new_value},
            quantity=data["available"],
            start_price=data["price"],
        )

    # Pictures must be set AFTER every add_variation_row() call.
    for old_value, new_value in OLD_TO_NEW.items():
        data = old_data[old_value]
        if data["pictures"]:
            set_variation_picture(variations, new_value, data["pictures"])

    xml = build_revise_xml(ITEM_ID, variations, "ReviseFixedPriceItem", account_num=ACCOUNT_NUM)
    root = _post("ReviseFixedPriceItem", xml, account_num=ACCOUNT_NUM)
    ack = _text(root, "Ack")
    print(f"ReviseFixedPriceItem Ack={ack}")
    if ack == "Failure":
        for e in _findall(root, "Errors"):
            print("  ERROR:", _text(e, "LongMessage") or _text(e, "ShortMessage"))
        raise SystemExit("Revise failed -- see errors above.")

    # Verify
    item2 = fetch_item(ITEM_ID, account_num=ACCOUNT_NUM)
    variations2 = deep_copy_variations(item2)
    live_count = len(_findall(variations2, "Variation"))
    print(f"\nLive <Variation> count after addition: {live_count} (expected 249)")

    live_by_value = {}
    for var in _findall(variations2, "Variation"):
        val = get_variation_specifics(var).get(specific_name)
        qty_el = _find(var, "Quantity")
        sold = int(_text(_find(var, "SellingStatus"), "QuantitySold", "0") or 0)
        stored = int(qty_el.text) if qty_el is not None and qty_el.text else 0
        live_by_value[val] = (stored, sold, stored - sold)

    for old_value, new_value in OLD_TO_NEW.items():
        if new_value in live_by_value:
            stored, sold, available = live_by_value[new_value]
            expected = old_data[old_value]["available"]
            status = "OK" if available == expected else "MISMATCH"
            print(f"  [{status}] {new_value}: available={available} (expected {expected}), stored={stored} sold={sold}")
        else:
            print(f"  MISSING: {new_value} not found live!")


if __name__ == "__main__":
    main()
