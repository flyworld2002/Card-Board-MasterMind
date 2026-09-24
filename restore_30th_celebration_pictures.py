"""
restore_30th_celebration_pictures.py -- one-off: rebuild EVERY variation's
<VariationSpecificPictureSet> on listing 336798749330 ("ME55 - 30th
Celebration Listing (Corrections)") from our own card_photos DB, in ONE
ReviseFixedPriceItem call -- not one call per card (that path,
push_all_photos_for_template(), is the known-slow/timeout-prone pattern
this codebase already flagged once before, 2026-09-18).

Recovery for: reorder_30th_celebration_variations.py's revise call
(2026-09-22) apparently sent an incomplete <Variations><Pictures> block
back to eBay, which eBay treated as "delete the rest" -- most
per-variation photos vanished from the live listing. Every roster row
already has card_photo_id set in our DB, so nothing is actually lost;
this rebuilds the live Pictures block from that source of truth.

Run once: python restore_30th_celebration_pictures.py
"""

from db.connection import db_cursor
from importer.card_photos import resolve_photo_urls
from importer.ebay import _find, _findall, _text, _post
from importer.ebay_variations_xml import (
    fetch_item, deep_copy_variations, strip_selling_status,
    get_specifics_set, set_variation_picture, build_revise_xml,
    assert_no_picture_loss,
)

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
    live_names = specifics_set[specific_name]
    print(f"Live variation count: {len(live_names)}")

    pics = _find(variations, "Pictures")
    before_count = len(_findall(pics, "VariationSpecificPictureSet")) if pics is not None else 0
    print(f"VariationSpecificPictureSet entries before restore: {before_count}")

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT elm.variation_name, lca.eps_picture_url, lca.card_photo_id
            FROM ebay_listing_map elm
            JOIN listing_card_assignments lca
                 ON lca.variant_id = elm.variant_id AND lca.template_id = %s
            WHERE elm.item_id = %s
            """,
            (TEMPLATE_ID, ITEM_ID),
        )
        rows_by_name = {r["variation_name"]: r for r in cur.fetchall()}

        missing = [n for n in live_names if n not in rows_by_name]
        if missing:
            print(f"WARNING: {len(missing)} live variation(s) have no roster row match, "
                  f"skipped: {missing}")

        restored, no_photo = 0, []
        for name in live_names:
            row = rows_by_name.get(name)
            if row is None:
                continue
            urls = resolve_photo_urls(cur, row)
            if not urls:
                no_photo.append(name)
                continue
            set_variation_picture(variations, name, urls)
            restored += 1

    print(f"Restoring pictures for {restored} of {len(live_names)} live variations "
          f"({len(no_photo)} have no photo source in DB).")
    if no_photo:
        print("  No photo source:", no_photo)

    assert_no_picture_loss(original_variations, variations)

    xml = build_revise_xml(ITEM_ID, variations, "ReviseFixedPriceItem", account_num=ACCOUNT_NUM)
    root = _post("ReviseFixedPriceItem", xml, account_num=ACCOUNT_NUM)
    ack = _text(root, "Ack")
    print(f"\nReviseFixedPriceItem Ack={ack}")
    if ack == "Warning":
        for e in _findall(root, "Errors"):
            print("  Warning:", _text(e, "LongMessage") or _text(e, "ShortMessage"))

    # Verify
    item2 = fetch_item(ITEM_ID, account_num=ACCOUNT_NUM)
    variations2 = deep_copy_variations(item2)
    pics2 = _find(variations2, "Pictures")
    after_count = len(_findall(pics2, "VariationSpecificPictureSet")) if pics2 is not None else 0
    print(f"VariationSpecificPictureSet entries after restore: {after_count}")


if __name__ == "__main__":
    main()
