"""
importer/rename_variation.py — Safely rename an eBay variation "VariationSpecifics"
value (e.g. fixing "Pokegear" -> "Pokégear", "PokÈ Ball" -> "Poké Ball", or
truncated/typo'd card names) WITHOUT touching quantity, price, SKU, or pictures.

Why this exists
----------------
eBay variation listings store the card name as a Value inside a
VariationSpecifics / NameValueList block (e.g. Name="Card Name", Value="Pokegear").
Typos, 50-char truncation, or missing accented characters in that Value break
the import pipeline's match against the Pokemon TCG API. ReviseItem can fix the
text, but a naive ReviseItem that doesn't resend the FULL Variations block
(VariationSpecificsSet + every Variation row + Pictures/VariationSpecificPictureSet)
risks eBay treating the omitted parts as deletions — wiping out other
variations, quantities, prices, or pictures.

This tool avoids that by:
  1. Fetching the item's full <Variations> block via GetItem (DetailLevel=ReturnAll).
  2. Deep-copying that exact XML element (preserving every SKU, Quantity,
     StartPrice, and Pictures entry untouched).
  3. Only rewriting the text of <Value> / <VariationSpecificValue> elements
     that exactly match the old name, for the one VariationSpecifics "Name"
     you specify (e.g. "Card Name").
  4. Stripping the GetItem-only <SellingStatus> sub-element (not valid on
     ReviseItem input) and leaving everything else byte-for-byte as eBay sent it.
  5. Sending that whole modified <Variations> block back via ReviseItem,
     ItemID + Variations only.
  6. Re-fetching afterward and printing a before/after diff for verification.

Safety model
------------
  - DRY RUN BY DEFAULT: `preview` never calls ReviseItem.
  - `apply` re-fetches fresh data, shows the full diff again, and requires you
    to type the exact new value to confirm before ReviseItem fires.
  - Renames ONE distinct VariationSpecifics value at a time (across however
    many SKU rows share that card name — e.g. Normal/Holo/Reverse Holo rows
    of the same card — never bulk-renaming multiple different card names).
  - Always re-verifies via a fresh GetItem after the change.

Usage
-----
    # See what VariationSpecifics names + values exist on a listing
    python -m importer.rename_variation list 334985403072

    # Dry run — show old -> new and every affected row, no API write
    python -m importer.rename_variation preview 334985403072 "Card Name" "Pokegear" "Pokégear"

    # Apply the rename (after reviewing the preview)
    python -m importer.rename_variation apply 334985403072 "Card Name" "Pokegear" "Pokégear"

RECOMMENDATION: run `apply` against a single low-stakes / low-quantity test
listing first, then re-check the listing on ebay.com (variation names,
pictures, quantity, price all unchanged) before using it on real inventory.
"""

import argparse
import copy
import sys
import xml.etree.ElementTree as ET

from importer.ebay import _post, _find, _findall, _text, NS
from importer.ebay_auth import get_user_token
from importer.ebay_variations_xml import fetch_item, EBAY_VARIATION_VALUE_MAX_LEN


# ══════════════════════════════════════════════════════════════════════════════
# Parsing / summarizing the Variations block for display
# ══════════════════════════════════════════════════════════════════════════════

def summarize(item: ET.Element) -> dict:
    """
    Build a human-readable summary of the item's variations.

    Returns:
    {
        "title": str,
        "has_variations": bool,
        "specifics_set": {name: [values, ...]},   # VariationSpecificsSet
        "variations": [
            {"sku": str|None, "quantity": str, "start_price": str,
             "currency": str|None, "qty_sold": str|None,
             "specifics": {name: value}},
            ...
        ],
        "picture_sets": [
            {"specific_name": str, "specific_value": str, "picture_urls": [str,...]},
            ...
        ],
    }
    """
    title = _text(item, "Title", "")
    variations_node = _find(item, "Variations")

    summary = {
        "title": title,
        "has_variations": variations_node is not None,
        "specifics_set": {},
        "variations": [],
        "picture_sets": [],
    }
    if variations_node is None:
        return summary

    # ── VariationSpecificsSet: the master list of {Name: [Values]} ──────────
    specifics_set_node = _find(variations_node, "VariationSpecificsSet")
    if specifics_set_node is not None:
        for nvl in _findall(specifics_set_node, "NameValueList"):
            name = _text(nvl, "Name")
            values = [v.text.strip() for v in _findall(nvl, "Value") if v.text]
            if name:
                summary["specifics_set"][name] = values

    # ── Each Variation row ───────────────────────────────────────────────────
    for var in _findall(variations_node, "Variation"):
        sku = _text(var, "SKU")
        qty = _text(var, "Quantity", "0")

        sp_el = _find(var, "StartPrice")
        start_price = sp_el.text.strip() if sp_el is not None and sp_el.text else None
        currency = sp_el.get("currencyID") if sp_el is not None else None

        selling = _find(var, "SellingStatus")
        qty_sold = _text(selling, "QuantitySold") if selling is not None else None

        specifics = {}
        specifics_node = _find(var, "VariationSpecifics")
        if specifics_node is not None:
            for nvl in _findall(specifics_node, "NameValueList"):
                name = _text(nvl, "Name")
                val_el = _find(nvl, "Value")
                value = val_el.text.strip() if val_el is not None and val_el.text else None
                if name:
                    specifics[name] = value

        summary["variations"].append({
            "sku": sku,
            "quantity": qty,
            "start_price": start_price,
            "currency": currency,
            "qty_sold": qty_sold,
            "specifics": specifics,
        })

    # ── Pictures / VariationSpecificPictureSet ───────────────────────────────
    pictures_node = _find(variations_node, "Pictures")
    if pictures_node is not None:
        pic_specific_name = _text(pictures_node, "VariationSpecificName")
        for pic_set in _findall(pictures_node, "VariationSpecificPictureSet"):
            value = _text(pic_set, "VariationSpecificValue")
            urls = [u.text.strip() for u in _findall(pic_set, "PictureURL") if u.text]
            summary["picture_sets"].append({
                "specific_name": pic_specific_name,
                "specific_value": value,
                "picture_urls": urls,
            })

    return summary


# ══════════════════════════════════════════════════════════════════════════════
# `list` command — show what specifics names/values exist
# ══════════════════════════════════════════════════════════════════════════════

def cmd_debug(item_id: str):
    """
    Dump raw counts and a sample of raw XML for the <Variations> block, to
    diagnose mismatches between VariationSpecificsSet and the actual
    <Variation> rows GetItem returns (e.g. ReviseItem complaining about a
    "Variation Specifics provided does not match" error).
    """
    item = fetch_item(item_id)
    variations_node = _find(item, "Variations")
    if variations_node is None:
        print("No <Variations> block at all.")
        return

    specifics_set_node = _find(variations_node, "VariationSpecificsSet")
    specifics_values = []
    specifics_name = None
    if specifics_set_node is not None:
        for nvl in _findall(specifics_set_node, "NameValueList"):
            specifics_name = _text(nvl, "Name")
            specifics_values = [v.text.strip() for v in _findall(nvl, "Value") if v.text]

    variation_elems = _findall(variations_node, "Variation")
    row_values = []
    for var in variation_elems:
        specifics_node = _find(var, "VariationSpecifics")
        if specifics_node is not None:
            for nvl in _findall(specifics_node, "NameValueList"):
                if _text(nvl, "Name") == specifics_name:
                    val_el = _find(nvl, "Value")
                    if val_el is not None and val_el.text:
                        row_values.append(val_el.text.strip())

    print(f"VariationSpecificsSet[{specifics_name!r}]: {len(specifics_values)} value(s)")
    print(f"<Variation> rows returned: {len(variation_elems)}")
    print(f"<Variation> rows with a {specifics_name!r} specific: {len(row_values)}")

    missing = [v for v in specifics_values if v not in row_values]
    if missing:
        print(f"\n{len(missing)} value(s) in VariationSpecificsSet have NO corresponding "
              f"<Variation> row in this GetItem response:")
        for m in missing:
            print(f"  - {m!r}")

    extra = [v for v in row_values if v not in specifics_values]
    if extra:
        print(f"\n{len(extra)} <Variation> row value(s) are NOT in VariationSpecificsSet:")
        for e in extra:
            print(f"  - {e!r}")

    # ── Top-level SellerProfiles / fulfillment hints ─────────────────────────
    for tag in ("SellerProfiles", "eBayPlusEligible"):
        el = _find(item, tag)
        if el is not None:
            print(f"\n<{tag}> present on item (raw):")
            print(ET.tostring(el, encoding="unicode"))

    # ── Raw XML for first variation, to check for SKU / other fields ────────
    if variation_elems:
        ET.register_namespace("", NS)
        print("\nRaw XML of first <Variation> row:")
        print(ET.tostring(variation_elems[0], encoding="unicode"))

    # ── Check Variations-level attributes (e.g. paging hints) ───────────────
    print("\n<Variations> top-level child tags (in order):")
    for child in variations_node:
        tag = child.tag.split("}")[-1]
        print(f"  - {tag}")


def cmd_list(item_id: str):
    item = fetch_item(item_id)
    summary = summarize(item)

    print(f"Item {item_id}: {summary['title']}")
    if not summary["has_variations"]:
        print("  This is a single (non-variation) listing — no VariationSpecifics to rename.")
        return

    print("\nVariationSpecifics names and values:")
    for name, values in summary["specifics_set"].items():
        print(f"  {name!r}:")
        for v in values:
            print(f"    - {v!r}")

    print(f"\n{len(summary['variations'])} variation row(s):")
    for v in summary["variations"]:
        specs = ", ".join(f"{k}={val!r}" for k, val in v["specifics"].items())
        sold = f", sold={v['qty_sold']}" if v["qty_sold"] else ""
        print(
            f"  SKU={v['sku']!r:<14} qty={v['quantity']:<4}{sold} "
            f"price={v['start_price']} {v['currency'] or ''}  [{specs}]"
        )

    if summary["picture_sets"]:
        print(f"\nPicture sets keyed on {summary['picture_sets'][0]['specific_name']!r}:")
        for ps in summary["picture_sets"]:
            print(f"  {ps['specific_value']!r}: {len(ps['picture_urls'])} picture(s)")


# ══════════════════════════════════════════════════════════════════════════════
# Diffing / matching
# ══════════════════════════════════════════════════════════════════════════════

def find_affected(summary: dict, specific_name: str, old_value: str) -> dict:
    """
    Returns:
    {
        "in_specifics_set": bool,
        "variation_indices": [int, ...],   # indices into summary["variations"]
        "picture_set_indices": [int, ...], # indices into summary["picture_sets"]
    }
    """
    result = {
        "in_specifics_set": old_value in summary["specifics_set"].get(specific_name, []),
        "variation_indices": [],
        "picture_set_indices": [],
    }

    for i, v in enumerate(summary["variations"]):
        if v["specifics"].get(specific_name) == old_value:
            result["variation_indices"].append(i)

    for i, ps in enumerate(summary["picture_sets"]):
        if ps["specific_name"] == specific_name and ps["specific_value"] == old_value:
            result["picture_set_indices"].append(i)

    return result


def print_preview(item_id: str, summary: dict, specific_name: str,
                   old_value: str, new_value: str, affected: dict):
    print(f"Item {item_id}: {summary['title']}")
    print(f"\nRename plan for VariationSpecifics[{specific_name!r}]:")
    print(f"  OLD VALUE: {old_value!r}")
    print(f"  NEW VALUE: {new_value!r}")

    if not affected["in_specifics_set"]:
        print(f"\n⚠️  {old_value!r} was NOT found in VariationSpecificsSet[{specific_name!r}].")
        print(f"   Available values: {summary['specifics_set'].get(specific_name)}")
        return

    if new_value in summary["specifics_set"].get(specific_name, []):
        print(f"\n⚠️  {new_value!r} ALREADY exists as a distinct value for "
              f"{specific_name!r}. Renaming to it would create a duplicate "
              f"value — this tool will refuse to apply this rename.")

    if len(new_value) > EBAY_VARIATION_VALUE_MAX_LEN:
        print(f"\n⚠️  New value is {len(new_value)} chars — eBay's "
              f"VariationSpecifics Value limit is {EBAY_VARIATION_VALUE_MAX_LEN}. "
              f"This will likely be rejected.")

    if not affected["variation_indices"]:
        print(f"\n⚠️  No Variation rows currently have {specific_name}={old_value!r}.")
        print("   (It's only listed in VariationSpecificsSet — nothing to rename on rows.)")

    print(f"\nAffected variation row(s): {len(affected['variation_indices'])}"
          " (these will be RESENT UNCHANGED except for the renamed value)")
    for i in affected["variation_indices"]:
        v = summary["variations"][i]
        sold = f", sold={v['qty_sold']}" if v["qty_sold"] else ""
        print(
            f"  SKU={v['sku']!r:<14} qty={v['quantity']:<4}{sold} "
            f"price={v['start_price']} {v['currency'] or ''}"
        )

    if affected["picture_set_indices"]:
        print(f"\nAffected picture set(s): {len(affected['picture_set_indices'])}"
              " (picture URLs unchanged, only the label is renamed)")
        for i in affected["picture_set_indices"]:
            ps = summary["picture_sets"][i]
            print(f"  {ps['specific_value']!r} -> {new_value!r}  "
                  f"({len(ps['picture_urls'])} picture(s))")
    else:
        print("\nNo picture sets are keyed on this value (pictures untouched either way).")

    print(f"\nEvery OTHER variation row, picture set, and value in "
          f"VariationSpecificsSet will be sent back to eBay byte-for-byte unchanged.")


# ══════════════════════════════════════════════════════════════════════════════
# Building the modified <Variations> block (in-place text rename only)
# ══════════════════════════════════════════════════════════════════════════════

def build_revised_variations(item: ET.Element, specific_name: str,
                              old_value: str, new_value: str) -> ET.Element:
    """
    Deep-copies <Variations> from a fresh GetItem response and rewrites ONLY
    the matching <Value> / <VariationSpecificValue> text nodes for
    `specific_name` == `old_value` -> `new_value`. Strips <SellingStatus>
    from each <Variation> (GetItem-output-only, invalid on ReviseItem input).
    Everything else — SKU, Quantity, StartPrice, Pictures, other specifics
    values/rows — is left exactly as eBay returned it.
    """
    variations_node = _find(item, "Variations")
    if variations_node is None:
        raise RuntimeError("Item has no <Variations> block — nothing to rename.")

    variations = copy.deepcopy(variations_node)
    renamed_any = False

    # ── VariationSpecificsSet ────────────────────────────────────────────────
    specifics_set_node = _find(variations, "VariationSpecificsSet")
    if specifics_set_node is not None:
        for nvl in _findall(specifics_set_node, "NameValueList"):
            if _text(nvl, "Name") != specific_name:
                continue
            for val_el in _findall(nvl, "Value"):
                if val_el.text and val_el.text.strip() == old_value:
                    val_el.text = new_value
                    renamed_any = True

    # ── Each Variation's VariationSpecifics, and strip SellingStatus ────────
    for var in _findall(variations, "Variation"):
        specifics_node = _find(var, "VariationSpecifics")
        if specifics_node is not None:
            for nvl in _findall(specifics_node, "NameValueList"):
                if _text(nvl, "Name") != specific_name:
                    continue
                val_el = _find(nvl, "Value")
                if val_el is not None and val_el.text and val_el.text.strip() == old_value:
                    val_el.text = new_value
                    renamed_any = True

        selling_status = _find(var, "SellingStatus")
        if selling_status is not None:
            var.remove(selling_status)

    # ── Pictures / VariationSpecificPictureSet ───────────────────────────────
    pictures_node = _find(variations, "Pictures")
    if pictures_node is not None and _text(pictures_node, "VariationSpecificName") == specific_name:
        for pic_set in _findall(pictures_node, "VariationSpecificPictureSet"):
            val_el = _find(pic_set, "VariationSpecificValue")
            if val_el is not None and val_el.text and val_el.text.strip() == old_value:
                val_el.text = new_value
                renamed_any = True

    if not renamed_any:
        raise RuntimeError(
            f"No occurrences of {specific_name}={old_value!r} were found to rename. "
            "Run the `list` command to see current values."
        )

    return variations


def build_revise_item_xml(item_id: str, variations: ET.Element) -> str:
    token = get_user_token()

    # Serialize the modified <Variations> subtree without ns0: prefixes —
    # ReviseItemRequest declares the same default namespace at the root.
    ET.register_namespace("", NS)
    variations_xml = ET.tostring(variations, encoding="unicode")

    return f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{item_id}</ItemID>
    {variations_xml}
  </Item>
</ReviseItemRequest>"""


# ══════════════════════════════════════════════════════════════════════════════
# `preview` command — dry run, no ReviseItem call
# ══════════════════════════════════════════════════════════════════════════════

def cmd_preview(item_id: str, specific_name: str, old_value: str, new_value: str):
    item = fetch_item(item_id)
    summary = summarize(item)

    if not summary["has_variations"]:
        print(f"Item {item_id} ({summary['title']}) is not a variation listing. Nothing to do.")
        return

    affected = find_affected(summary, specific_name, old_value)
    print_preview(item_id, summary, specific_name, old_value, new_value, affected)
    print("\n(DRY RUN — no changes were made. Re-run with `apply` to make this change.)")


# ══════════════════════════════════════════════════════════════════════════════
# `apply` command — confirm, ReviseItem, then re-verify
# ══════════════════════════════════════════════════════════════════════════════

def cmd_apply(item_id: str, specific_name: str, old_value: str, new_value: str):
    print("Fetching current listing state...\n")
    item = fetch_item(item_id)
    summary = summarize(item)

    if not summary["has_variations"]:
        print(f"Item {item_id} ({summary['title']}) is not a variation listing. Nothing to do.")
        return

    affected = find_affected(summary, specific_name, old_value)
    print_preview(item_id, summary, specific_name, old_value, new_value, affected)

    if not affected["in_specifics_set"] or not affected["variation_indices"]:
        print("\n❌ Aborting — nothing valid to rename (see warnings above).")
        return

    if new_value in summary["specifics_set"].get(specific_name, []):
        print(f"\n❌ Aborting — {new_value!r} already exists as a distinct value; "
              "renaming to it would create a duplicate.")
        return

    if len(new_value) > EBAY_VARIATION_VALUE_MAX_LEN:
        print(f"\n❌ Aborting — new value exceeds eBay's "
              f"{EBAY_VARIATION_VALUE_MAX_LEN}-character limit.")
        return

    # ── Explicit confirmation: type the exact new value ─────────────────────
    print(f"\nThis will call ReviseItem on item {item_id}, renaming "
          f"{len(affected['variation_indices'])} row(s) and "
          f"{len(affected['picture_set_indices'])} picture set(s) "
          f"from {old_value!r} to {new_value!r}.")
    print("Quantities, prices, SKUs, and picture URLs will be resent unchanged.")
    confirm = input(f"\nType the new value exactly to confirm ({new_value!r}): ")
    if confirm != new_value:
        print("❌ Confirmation did not match. Aborting — no changes made.")
        return

    # ── Build payload from a FRESH copy of the just-fetched data ────────────
    # (re-fetch here too, in case time has passed / another process changed it)
    print("\nRe-fetching item immediately before ReviseItem (to avoid stale data)...")
    item = fetch_item(item_id)
    variations = build_revised_variations(item, specific_name, old_value, new_value)
    revise_xml = build_revise_item_xml(item_id, variations)

    print("Calling ReviseItem...")
    _post("ReviseItem", revise_xml)
    print("✅ ReviseItem succeeded.")

    # ── Verify ────────────────────────────────────────────────────────────
    print("\nRe-fetching item to verify...")
    new_item = fetch_item(item_id)
    new_summary = summarize(new_item)

    old_values = new_summary["specifics_set"].get(specific_name, [])
    if new_value in old_values and old_value not in old_values:
        print(f"✅ VariationSpecificsSet[{specific_name!r}] now contains "
              f"{new_value!r} and no longer contains {old_value!r}.")
    else:
        print(f"⚠️  Unexpected VariationSpecificsSet[{specific_name!r}] values: {old_values}")

    print("\nRow-by-row verification (qty/price should be unchanged from before):")
    before_by_sku = {v["sku"]: v for v in summary["variations"]}
    for v in new_summary["variations"]:
        if v["specifics"].get(specific_name) == new_value:
            before = before_by_sku.get(v["sku"], {})
            qty_match = before.get("quantity") == v["quantity"]
            price_match = before.get("start_price") == v["start_price"]
            status = "✅" if (qty_match and price_match) else "⚠️ "
            print(
                f"  {status} SKU={v['sku']!r:<14} "
                f"qty: {before.get('quantity')} -> {v['quantity']}  "
                f"price: {before.get('start_price')} -> {v['start_price']}"
            )

    if new_summary["picture_sets"]:
        before_urls = {
            ps["specific_value"]: ps["picture_urls"] for ps in summary["picture_sets"]
        }
        for ps in new_summary["picture_sets"]:
            if ps["specific_value"] == new_value:
                prior = before_urls.get(old_value, [])
                match = "✅" if prior == ps["picture_urls"] else "⚠️ "
                print(f"  {match} Picture set {new_value!r}: "
                      f"{len(ps['picture_urls'])} picture(s) "
                      f"(was {len(prior)} under {old_value!r})")

    print("\nDone. Recommended: also open the listing on ebay.com and confirm "
          "the variation dropdown shows the corrected name with its original picture.")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Rename an eBay variation's VariationSpecifics value "
                     "(e.g. fix accent/typo/truncation) without touching "
                     "quantity, price, SKU, or pictures."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Show VariationSpecifics names/values for an item")
    p_list.add_argument("item_id")

    p_debug = sub.add_parser("debug", help="Dump raw Variations XML/counts to diagnose mismatches")
    p_debug.add_argument("item_id")

    p_preview = sub.add_parser("preview", help="Dry run — show old -> new diff, no API write")
    p_preview.add_argument("item_id")
    p_preview.add_argument("specific_name", help='e.g. "Card Name"')
    p_preview.add_argument("old_value")
    p_preview.add_argument("new_value")

    p_apply = sub.add_parser("apply", help="Apply the rename via ReviseItem (with confirmation)")
    p_apply.add_argument("item_id")
    p_apply.add_argument("specific_name", help='e.g. "Card Name"')
    p_apply.add_argument("old_value")
    p_apply.add_argument("new_value")

    args = parser.parse_args()

    try:
        if args.command == "list":
            cmd_list(args.item_id)
        elif args.command == "debug":
            cmd_debug(args.item_id)
        elif args.command == "preview":
            cmd_preview(args.item_id, args.specific_name, args.old_value, args.new_value)
        elif args.command == "apply":
            cmd_apply(args.item_id, args.specific_name, args.old_value, args.new_value)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
