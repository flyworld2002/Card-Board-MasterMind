"""
test_delete_variation.py — one-off diagnostic: does ReviseFixedPriceItem's
Variation.Delete=true actually remove a PURCHASED variation via the API,
the same way Seller Hub just did manually (item 334903449758, 250->249,
confirmed gone)?

This is deliberately a standalone script, not wired into main.py — it's a
rare, destructive, one-time verification step for the eBay listing sync
plan (Step 0 #2), not part of normal operation.

⚠️ DESTRUCTIVE: this will actually delete a variation from a LIVE listing
via the API. Only run against a variation you're fully willing to lose
(ideally the exact kind of case the plan cares about: qty already 0, has
at least one past sale). Recommend picking a different sacrificial
variation than the one already removed via Seller Hub, so this is a clean
second, independent data point.

USAGE:
    python3 test_delete_variation.py --item 334903449758 --variation "NNN/165 Some Card Name"

    Add --dry-run to only print what WOULD be sent (no API call at all).

What it does:
  1. GetItem — fetch current Variations, find the row matching --variation
     by name, print its current Quantity and confirm it exists.
  2. Confirm before sending anything live (unless --yes is passed).
  3. ReviseFixedPriceItem — deep-copies the full <Variations> block
     (same pattern as rename_variation.py: strip <SellingStatus>, which is
     GetItem-only and rejected on revise), sets Variation.Delete=true on
     the target row only, sends the revise.
  4. GetItem again — confirms whether the variation is truly gone from
     the XML, or still present (e.g. silently forced to qty 0 instead).
  5. Prints a clear PASS/FAIL verdict for the plan's open question.
"""

import argparse
import copy

from importer.ebay import _post, _find, _findall, _text
from importer.ebay_auth import get_user_token


def _get_variations(item_id: str, account_num: int):
    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{get_user_token(account_num)}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""

    root = _post("GetItem", xml_body, account_num=account_num)
    item = _find(root, "Item")
    variations_block = _find(item, "Variations")
    if variations_block is None:
        raise SystemExit(f"Item {item_id} has no <Variations> block — not a multi-variation listing?")
    rows = _findall(variations_block, "Variation")
    return root, item, variations_block, rows


def _variation_name(variation_row):
    """Concatenate NameValueList Value(s) under VariationSpecifics — best-effort,
    matches however the app already displays a variation's identity elsewhere."""
    specifics = _find(variation_row, "VariationSpecifics")
    if specifics is None:
        return None
    values = []
    for nvl in _findall(specifics, "NameValueList"):
        val = _text(nvl, "Value")
        if val:
            values.append(val)
    return " / ".join(values) if values else None


def find_and_report(item_id: str, target_name: str, account_num: int):
    _, item, variations_block, rows = _get_variations(item_id, account_num)
    print(f"Item {item_id}: {len(rows)} variation row(s) currently.")

    match = None
    for row in rows:
        name = _variation_name(row)
        if name and target_name.strip().lower() in name.strip().lower():
            match = row
            found_name = name
            break

    if match is None:
        print(f"\n❌ No variation matching '{target_name}' found. Names seen:")
        for row in rows[:15]:
            print(f"   - {_variation_name(row)}")
        if len(rows) > 15:
            print(f"   ... ({len(rows) - 15} more)")
        raise SystemExit(1)

    qty = _text(match, "Quantity", "?")
    selling_status = _find(match, "SellingStatus")
    qty_sold = _text(selling_status, "QuantitySold", "0") if selling_status is not None else "0"
    try:
        available = int(qty) - int(qty_sold)
    except ValueError:
        available = None
    print(f"\n✅ Found target variation: '{found_name}'")
    print(f"   Quantity (total ever listed): {qty}")
    print(f"   QuantitySold: {qty_sold}")
    print(f"   Available (Quantity - QuantitySold): {available if available is not None else '?'}")
    if available is not None and available != 0:
        print(f"   ⚠️  NOT sold out — deleting this would remove {available} real sellable unit(s).")
    return match, found_name


def delete_variation(item_id: str, target_name: str, account_num: int, dry_run: bool):
    root, item, variations_block, rows = _get_variations(item_id, account_num)

    match = None
    for row in rows:
        name = _variation_name(row)
        if name and target_name.strip().lower() in name.strip().lower():
            match = row
            found_name = name
            break

    if match is None:
        raise SystemExit(f"Variation '{target_name}' not found at revise time (did it change since the check?).")

    # Deep-copy the full Variations block, same pattern as rename_variation.py.
    variations_copy = copy.deepcopy(variations_block)

    # SellingStatus is GetItem-only — must be stripped before revise, per
    # the existing rename_variation.py gotcha, or eBay rejects the call.
    for row_copy in _findall(variations_copy, "Variation"):
        selling_status = _find(row_copy, "SellingStatus")
        if selling_status is not None:
            row_copy.remove(selling_status)

    # Mark ONLY the target row for deletion.
    target_copy = None
    for row_copy in _findall(variations_copy, "Variation"):
        if target_name.strip().lower() in (_variation_name(row_copy) or "").strip().lower():
            target_copy = row_copy
            break
    if target_copy is None:
        raise SystemExit("Lost track of the target row while building the revise payload — aborting, nothing sent.")

    import xml.etree.ElementTree as ET
    delete_flag = ET.SubElement(target_copy, "Delete")
    delete_flag.text = "true"

    variations_xml = ET.tostring(variations_copy, encoding="unicode")

    xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{get_user_token(account_num)}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{item_id}</ItemID>
    {variations_xml}
  </Item>
</ReviseFixedPriceItemRequest>"""

    print(f"\n--- Revise payload for '{found_name}' (Delete=true) ---")
    print(xml_body)
    print("--- end payload ---\n")

    if dry_run:
        print("🔎 --dry-run: not sending. Nothing changed.")
        return

    resp_root = _post("ReviseFixedPriceItem", xml_body, account_num=account_num)
    ack = _text(resp_root, "Ack", "?")
    print(f"ReviseFixedPriceItem Ack: {ack}")

    errors = _findall(resp_root, "Errors")
    for err in errors:
        print(f"  [{_text(err, 'SeverityCode', '?')}] {_text(err, 'ShortMessage', '')} — {_text(err, 'LongMessage', '')}")

    if ack not in ("Success", "Warning"):
        print("\n❌ Revise call failed or was rejected. See errors above.")
        return

    print("\n✅ Revise accepted. Re-checking via GetItem...\n")
    verify(item_id, target_name, account_num)


def verify(item_id: str, target_name: str, account_num: int):
    _, item, variations_block, rows = _get_variations(item_id, account_num)
    print(f"Item {item_id}: {len(rows)} variation row(s) now.")

    for row in rows:
        name = _variation_name(row)
        if name and target_name.strip().lower() in name.strip().lower():
            qty = _text(row, "Quantity", "?")
            print(f"\n⚠️  RESULT: variation STILL PRESENT (Quantity={qty}).")
            print("   => eBay's docs are accurate for the API path (even though Seller Hub")
            print("      let it through) — fall back to qty-0 + the 250-cap-counting question.")
            return

    print(f"\n✅ RESULT: variation is GONE. Deletion via the API worked for a purchased row.")
    print("   => Promotion (delete sold-out + add queued card) is viable as originally designed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--item", required=True, help="eBay ItemID of the listing")
    parser.add_argument("--variation", required=True, help="Substring of the target variation's name (e.g. card name or number)")
    parser.add_argument("--account", type=int, default=1)
    parser.add_argument("--check-only", action="store_true", help="Just look up and report the variation, don't delete anything")
    parser.add_argument("--dry-run", action="store_true", help="Print the revise payload but don't send it")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt")
    args = parser.parse_args()

    if args.check_only:
        find_and_report(args.item, args.variation, args.account)
        raise SystemExit(0)

    _, found_name = find_and_report(args.item, args.variation, args.account)

    if not args.dry_run and not args.yes:
        confirm = input(
            f"\n⚠️  About to attempt DELETING '{found_name}' from item {args.item} via the API. "
            f"Type 'yes' to proceed: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted, nothing sent.")
            raise SystemExit(0)

    delete_variation(args.item, args.variation, args.account, dry_run=args.dry_run)
