"""
audit_variation_pictures.py -- one-off, READ-ONLY audit: for every
multi-variation eBay listing this app tracks, compare the LIVE
<Variations><Pictures><VariationSpecificPictureSet> count (via GetItem)
against how many of that listing's live roster rows have a photo source
(card_photo_id/eps_picture_url) in our DB. Flags any listing where live
count is short -- the same symptom the 2026-09-22 "30th Celebration"
incident had (a revise call silently dropped most per-variation photos).

No writes, no ReviseItem/ReviseFixedPriceItem calls -- GetItem only.

Run: python audit_variation_pictures.py
"""

from db.connection import db_cursor
from importer.ebay import _find, _findall
from importer.ebay_variations_xml import fetch_item, deep_copy_variations, get_specifics_set

ACCOUNT_NUM = 1


def main():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT pl.listing_id,
                   count(*) AS live_roster_rows,
                   count(*) FILTER (WHERE lca.card_photo_id IS NOT NULL
                                         OR lca.eps_picture_url IS NOT NULL) AS rows_with_photo_source
            FROM platform_listings pl
            JOIN listing_card_assignments lca ON lca.platform_listing_id = pl.id
            WHERE pl.status IN ('active','out_of_stock') AND pl.platform = 'ebay'
              AND pl.account IS NOT NULL
            GROUP BY pl.listing_id
            ORDER BY rows_with_photo_source DESC
            """
        )
        listings = cur.fetchall()

    print(f"Auditing {len(listings)} live listings...\n")

    flagged = []
    errors = []
    for i, row in enumerate(listings, 1):
        listing_id = row["listing_id"]
        expected = row["rows_with_photo_source"]
        try:
            item = fetch_item(listing_id, account_num=ACCOUNT_NUM)
            variations = deep_copy_variations(item)
        except Exception as e:
            errors.append({"listing_id": listing_id, "error": str(e)})
            print(f"[{i}/{len(listings)}] {listing_id}: ERROR fetching -- {e}")
            continue

        pics = _find(variations, "Pictures")
        actual = len(_findall(pics, "VariationSpecificPictureSet")) if pics is not None else 0

        status = "OK" if actual >= expected else "MISMATCH"
        print(f"[{i}/{len(listings)}] {listing_id}: expected>={expected}, live={actual}  [{status}]")

        if actual < expected:
            flagged.append({"listing_id": listing_id, "expected": expected, "actual": actual,
                             "missing": expected - actual})

    print("\n" + "=" * 60)
    if flagged:
        print(f"FLAGGED -- {len(flagged)} listing(s) with fewer live pictures than expected:")
        for f in flagged:
            print(f"  {f['listing_id']}: expected>={f['expected']}, live={f['actual']} "
                  f"(missing ~{f['missing']})")
    else:
        print("No other listings affected -- every one matches or exceeds its expected photo count.")

    if errors:
        print(f"\n{len(errors)} listing(s) could not be checked (fetch errors):")
        for e in errors:
            print(f"  {e['listing_id']}: {e['error']}")


if __name__ == "__main__":
    main()
