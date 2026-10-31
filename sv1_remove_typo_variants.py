r"""
sv1_remove_typo_variants.py -- one-off, step 1 of 2: remove the 5
typo'd <Variation> rows from listing 364198382892, following Fei's
explicit instruction to remove-then-add rather than rename in place.

Uses the EXACT proven pattern from importer/ebay_pushprices.py's
remove_single_card_live() (2026-09-12 large-listing fix): strips the
WHOLE <Pictures> block from the outgoing payload before posting, then
mark_variation_deleted(..., prune_menu=False) since there's no Pictures
block left to prune a value from. in-place rename (changing both the
VariationSpecifics text AND the matching Pictures VariationSpecificValue
in one request) repeatedly failed with eBay's "Variation Specifics
provided does not match" error on this listing even after the
quantity-inflation and stale-menu bugs were both fixed -- this sidesteps
that entirely by using an already-proven removal path instead.

Step 2 (sv1_add_corrected_variants.py) re-adds the 5 rows under their
correct names using the qty/price/picture captured in
sv1_old_variant_data.json BEFORE this script runs.
"""

from importer.ebay import _find, _findall, _text, _post
from importer.ebay_variations_xml import (
    fetch_item, deep_copy_variations, normalize_quantities, strip_selling_status,
    get_specifics_set, find_variation_by_specifics, mark_variation_deleted,
    build_revise_xml,
)

ITEM_ID = "364198382892"
ACCOUNT_NUM = 2

OLD_VALUES = [
    "029/198 Scovillian Reverse Holo RH",
    "169/198 Defiant Band Reverse Holo RH",
    "170/198 Electricity Generator Reverse Holo RH",
    "178/198 Mesagosa Reverse Holo RH",
    "214/198 Grevard",
]


def main():
    item = fetch_item(ITEM_ID, account_num=ACCOUNT_NUM)
    variations = deep_copy_variations(item)
    normalize_quantities(variations)
    strip_selling_status(variations)

    # Same fix remove_single_card_live() uses for large listings -- strip
    # Pictures entirely rather than resend it (modified or not).
    pictures_node = _find(variations, "Pictures")
    if pictures_node is not None:
        variations.remove(pictures_node)

    specifics_set = get_specifics_set(variations)
    specific_name = next(iter(specifics_set), None)
    if not specific_name:
        raise SystemExit("No VariationSpecificsSet found on this listing.")

    not_found = []
    for old_value in OLD_VALUES:
        var_el = find_variation_by_specifics(variations, specific_name, old_value)
        if var_el is None:
            not_found.append(old_value)
            continue
        mark_variation_deleted(variations, var_el, specific_name, prune_menu=False)

    if not_found:
        raise SystemExit(f"Aborting -- {len(not_found)} value(s) not found live: {not_found}")

    print(f"Marked {len(OLD_VALUES)} variation(s) for deletion.")

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
    print(f"\nLive <Variation> count after removal: {live_count} (expected 244)")

    live_values = set()
    for var in _findall(variations2, "Variation"):
        from importer.ebay_variations_xml import get_variation_specifics
        live_values.add(get_variation_specifics(var).get(specific_name))

    still_live = [v for v in OLD_VALUES if v in live_values]
    if still_live:
        print(f"WARNING -- still live after removal: {still_live}")
    else:
        print("Confirmed: all 5 old values are no longer live.")


if __name__ == "__main__":
    main()
