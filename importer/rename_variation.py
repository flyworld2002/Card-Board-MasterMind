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

from db.connection import db_cursor
from importer.ebay import _post, _find, _findall, _text, NS
from importer.ebay_auth import get_user_token
from importer.ebay_variations_xml import (
    fetch_item, build_revise_xml, EBAY_VARIATION_VALUE_MAX_LEN,
    deep_copy_variations, normalize_quantities, strip_selling_status,
    find_variation_by_specifics, get_specifics_set, insert_specifics_value,
    add_variation_row, set_variation_picture,
)


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
# `bulk-preview` / `bulk-apply` — resync MANY variation names on one listing
# to whatever the render_variation_name() Postgres RPC currently computes,
# in ONE ReviseItem call, instead of one apply per value. Built 2026-09-02
# for a listing whose card_sets.base_set_number was corrected after the
# listing was first pushed, leaving 130 live variation names stale — but
# not scoped to that one cause: this resyncs against whatever the RPC
# says right now, so it's the right tool for any future case of the same
# "DB says one thing, eBay's live text says another" drift (already hit
# twice this session — this listing, and a single-card version of the
# same problem on a Stellar Crown listing fixed by hand).
# ══════════════════════════════════════════════════════════════════════════════

def compute_bulk_renames(item_id: str, specific_name: str, only: list[str] = None):
    """
    Returns (item, summary, renames, needs_reconcile):
      item / summary   - fresh GetItem + its summarize() (same as preview/apply)
      renames           - {current_live_value: correct_value} for every row
                           whose stored platform_listings.external_id no
                           longer matches what render_variation_name() computes
      needs_reconcile   - platform_listings rows whose external_id doesn't
                           match ANY live VariationSpecifics value at all
                           (stale beyond a simple rename — same "needs manual
                           reconcile in Seller Hub" case this codebase already
                           flags elsewhere) -- never included in `renames`.
    `only`, if given, is a list of card_number strings (matched against
    card_master.card_number_numeric) restricting the batch to just those
    rows -- everything else is left out of `renames` entirely, even if it
    would otherwise need renaming. Used for the staged 1-card / 2-card /
    rest rollout instead of always touching every mismatched row.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT id FROM listing_templates WHERE listing_id = %s AND platform = 'ebay'",
            (item_id,),
        )
        template_row = cur.fetchone()
        if template_row is None:
            raise RuntimeError(f"No listing_templates row found for eBay item {item_id!r}.")
        template_id = template_row["id"]

        cur.execute(
            """
            SELECT pl.id AS pl_id, pl.external_id, cm.card_number, cm.card_number_numeric,
                   render_variation_name(pl.variant_id, %s) AS correct_name
            FROM platform_listings pl
            JOIN card_variants cv ON pl.variant_id = cv.id
            JOIN card_master cm ON cv.card_id = cm.id
            WHERE pl.listing_id = %s AND pl.status IN ('active', 'out_of_stock')
            """,
            (template_id, item_id),
        )
        rows = cur.fetchall()

    if only:
        only_set = {str(int(n)) for n in only}
        rows = [r for r in rows if str(r["card_number_numeric"]) in only_set]

    item = fetch_item(item_id)
    summary = summarize(item)
    live_values = set(summary["specifics_set"].get(specific_name, []))

    renames = {}
    needs_reconcile = []
    for r in rows:
        if not r["external_id"] or r["external_id"] == r["correct_name"]:
            continue
        if r["external_id"] not in live_values:
            needs_reconcile.append(r)
            continue
        renames[r["external_id"]] = r["correct_name"]

    return item, summary, renames, needs_reconcile


def fetch_sku_map(item_id: str) -> dict:
    """
    {external_id: platform_listings.id} for EVERY active/out_of_stock row
    on this listing -- deliberately ignores any `--only` filter, since
    "SKU required in every variation" (found 2026-09-02 on this listing)
    means every row in the resent <Variations> block needs one, not just
    whichever row(s) a given bulk-apply call happens to be renaming.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, external_id FROM platform_listings "
            "WHERE listing_id = %s AND status IN ('active', 'out_of_stock') AND external_id IS NOT NULL",
            (item_id,),
        )
        return {row["external_id"]: str(row["id"]) for row in cur.fetchall()}


def print_bulk_preview(item_id: str, summary: dict, specific_name: str, renames: dict, needs_reconcile: list):
    print(f"Item {item_id}: {summary['title']}")
    if not renames and not needs_reconcile:
        print("\nNothing to rename — every row already matches render_variation_name().")
        return

    print(f"\n{len(renames)} row(s) to rename:")
    for old_value, new_value in renames.items():
        print(f"  {old_value!r} -> {new_value!r}")

    existing_values = set(summary["specifics_set"].get(specific_name, []))
    dupes = set(renames.values()) & (existing_values - set(renames.keys()))
    if dupes:
        print(f"\n⚠️  {len(dupes)} new value(s) already exist as a DIFFERENT distinct value — "
              f"this tool will refuse to apply: {sorted(dupes)}")

    too_long = {v for v in renames.values() if len(v) > EBAY_VARIATION_VALUE_MAX_LEN}
    if too_long:
        print(f"\n⚠️  {len(too_long)} new value(s) exceed eBay's {EBAY_VARIATION_VALUE_MAX_LEN}-char "
              f"limit and will likely be rejected: {sorted(too_long)}")

    if needs_reconcile:
        print(f"\n{len(needs_reconcile)} row(s) skipped — their stored external_id doesn't match "
              "ANY live value right now (stale beyond a rename, needs manual reconcile):")
        for r in needs_reconcile:
            print(f"  card_number={r['card_number']!r} external_id={r['external_id']!r}")


def cmd_bulk_preview(item_id: str, specific_name: str, only: list[str] = None):
    item, summary, renames, needs_reconcile = compute_bulk_renames(item_id, specific_name, only)
    if not summary["has_variations"]:
        print(f"Item {item_id} ({summary['title']}) is not a variation listing. Nothing to do.")
        return
    print_bulk_preview(item_id, summary, specific_name, renames, needs_reconcile)
    print("\n(DRY RUN — no changes were made. Re-run with `bulk-apply` to make this change.)")


def build_revised_variations_bulk(item: ET.Element, specific_name: str, renames: dict,
                                   sku_by_value: dict = None) -> tuple:
    """
    Same contract as build_revised_variations() (deep-copy <Variations>,
    strip <SellingStatus>, everything except the renamed text nodes sent
    back byte-for-byte unchanged) generalized to rewrite every
    old_value -> new_value pair in `renames` in ONE pass instead of one
    old/new pair at a time.

    Also drops any VariationSpecificsSet[specific_name] value that has NO
    corresponding live <Variation> row at all -- eBay's ReviseItem
    rejects the whole call outright ("Variation Specifics provided does
    not match...") if the declared set and the actual rows disagree, and
    this codebase already has real examples of a specifics set
    accumulating orphaned values over time (old/removed variations never
    cleaned up). Nothing live references an orphan value by definition,
    so dropping it changes nothing about any real row's name, qty,
    price, or picture.

    `sku_by_value`, if given, maps each row's CURRENT (pre-rename)
    specifics text -> the SKU to assign it (only for rows that don't
    already have one) -- found 2026-09-02 that this particular listing's
    ReviseItem calls are rejected outright ("SKU is required in every
    variation for items that are fulfilled by eBay") until every
    variation carries a SKU; since ReviseItem always resends the WHOLE
    <Variations> block, this has to be set on every row in the same call
    as any rename, not just the row(s) actually being renamed. Uses each
    row's own platform_listings.id (a UUID, guaranteed unique, needs no
    new column or naming scheme) as its SKU -- see cmd_bulk_apply.

    Returns (variations, renamed_count, dropped_orphans, skus_added).
    """
    variations_node = _find(item, "Variations")
    if variations_node is None:
        raise RuntimeError("Item has no <Variations> block — nothing to rename.")

    variations = copy.deepcopy(variations_node)
    renamed_count = 0
    skus_added = 0

    live_row_values = set()
    for var in _findall(variations, "Variation"):
        specifics_node = _find(var, "VariationSpecifics")
        if specifics_node is None:
            continue
        for nvl in _findall(specifics_node, "NameValueList"):
            if _text(nvl, "Name") != specific_name:
                continue
            val_el = _find(nvl, "Value")
            if val_el is not None and val_el.text:
                live_row_values.add(val_el.text.strip())

    dropped_orphans = []
    specifics_set_node = _find(variations, "VariationSpecificsSet")
    if specifics_set_node is not None:
        for nvl in _findall(specifics_set_node, "NameValueList"):
            if _text(nvl, "Name") != specific_name:
                continue
            for val_el in list(_findall(nvl, "Value")):
                text = val_el.text.strip() if val_el.text else None
                if text is not None and text not in live_row_values:
                    nvl.remove(val_el)
                    dropped_orphans.append(text)
                    continue
                if text in renames:
                    val_el.text = renames[text]
                    renamed_count += 1

    for var in _findall(variations, "Variation"):
        original_value = None
        specifics_node = _find(var, "VariationSpecifics")
        if specifics_node is not None:
            for nvl in _findall(specifics_node, "NameValueList"):
                if _text(nvl, "Name") != specific_name:
                    continue
                val_el = _find(nvl, "Value")
                if val_el is not None and val_el.text:
                    original_value = val_el.text.strip()
                    if original_value in renames:
                        val_el.text = renames[original_value]
                        renamed_count += 1

        if sku_by_value and original_value in sku_by_value and _find(var, "SKU") is None:
            sku_el = ET.Element(f"{{{NS}}}SKU")
            sku_el.text = sku_by_value[original_value]
            var.insert(0, sku_el)
            skus_added += 1

        selling_status = _find(var, "SellingStatus")
        if selling_status is not None:
            var.remove(selling_status)

    pictures_node = _find(variations, "Pictures")
    if pictures_node is not None and _text(pictures_node, "VariationSpecificName") == specific_name:
        for pic_set in _findall(pictures_node, "VariationSpecificPictureSet"):
            val_el = _find(pic_set, "VariationSpecificValue")
            if val_el is not None and val_el.text and val_el.text.strip() in renames:
                val_el.text = renames[val_el.text.strip()]
                renamed_count += 1

    if renamed_count == 0:
        raise RuntimeError("None of the values in `renames` were found to rename on this item.")

    return variations, renamed_count, dropped_orphans, skus_added


def cmd_bulk_apply(item_id: str, specific_name: str, only: list[str] = None, assign_sku: bool = False,
                    use_revise_item: bool = False):
    print("Fetching current listing state...\n")
    item, summary, renames, needs_reconcile = compute_bulk_renames(item_id, specific_name, only)

    if not summary["has_variations"]:
        print(f"Item {item_id} ({summary['title']}) is not a variation listing. Nothing to do.")
        return

    print_bulk_preview(item_id, summary, specific_name, renames, needs_reconcile)

    if not renames:
        print("\n❌ Aborting — nothing to rename.")
        return

    existing_values = set(summary["specifics_set"].get(specific_name, []))
    dupes = {v for v in renames.values() if v in existing_values and v not in renames}
    if dupes:
        print(f"\n❌ Aborting — {len(dupes)} new value(s) already exist as a distinct value: {sorted(dupes)}")
        return
    too_long = {v for v in renames.values() if len(v) > EBAY_VARIATION_VALUE_MAX_LEN}
    if too_long:
        print(f"\n❌ Aborting — {len(too_long)} new value(s) exceed eBay's "
              f"{EBAY_VARIATION_VALUE_MAX_LEN}-character limit.")
        return

    print(f"\nThis will call ReviseItem ONCE on item {item_id}, renaming {len(renames)} row(s).")
    print("Quantities, prices, SKUs, and picture URLs will be resent unchanged.")
    confirm = input(f"\nType the row count to confirm ({len(renames)}): ")
    if confirm != str(len(renames)):
        print("❌ Confirmation did not match. Aborting — no changes made.")
        return

    sku_by_value = None
    if assign_sku:
        sku_by_value = fetch_sku_map(item_id)
        print(f"\n--assign-sku: will set a SKU (= each row's own platform_listings.id) on every "
              f"variation that doesn't already have one, up to {len(sku_by_value)} row(s) — "
              f"required by eBay for this listing to accept ANY ReviseItem call right now.")

    print("\nRe-fetching item immediately before ReviseItem (to avoid stale data)...")
    item = fetch_item(item_id)
    variations, renamed_count, dropped_orphans, skus_added = build_revised_variations_bulk(
        item, specific_name, renames, sku_by_value)
    if dropped_orphans:
        print(f"\nNote: dropping {len(dropped_orphans)} orphaned VariationSpecificsSet value(s) "
              f"with no corresponding live <Variation> row (harmless — nothing live references "
              f"them): {dropped_orphans}")
    if skus_added:
        print(f"Note: assigned a SKU to {skus_added} variation row(s) that didn't have one.")

    call_name = "ReviseItem" if use_revise_item else "ReviseFixedPriceItem"
    revise_xml = build_revise_xml(item_id, variations, call_name)

    print(f"Calling {call_name}...")
    _post(call_name, revise_xml)
    print(f"✅ {call_name} succeeded.")

    print(f"\nUpdating platform_listings.external_id for {len(renames)} row(s)...")
    with db_cursor() as cur:
        for old_value, new_value in renames.items():
            cur.execute(
                "UPDATE platform_listings SET external_id = %s "
                "WHERE listing_id = %s AND external_id = %s",
                (new_value, item_id, old_value),
            )
    print("✅ DB updated.")

    print("\nRe-fetching item to verify...")
    new_item = fetch_item(item_id)
    new_summary = summarize(new_item)
    new_values = set(new_summary["specifics_set"].get(specific_name, []))
    old_values_gone = [v for v in renames.keys() if v in new_values]
    new_values_present = [v for v in renames.values() if v not in new_values]
    if not old_values_gone and not new_values_present:
        print(f"✅ All {len(renames)} renamed value(s) now live, none of the old values remain.")
    else:
        if old_values_gone:
            print(f"⚠️  {len(old_values_gone)} old value(s) still present: {old_values_gone}")
        if new_values_present:
            print(f"⚠️  {len(new_values_present)} new value(s) NOT found live: {new_values_present}")

    print("\nDone. Recommended: also open the listing on ebay.com and spot-check a few "
          "renamed variations (name, quantity, price, picture).")


# ══════════════════════════════════════════════════════════════════════════════
# `add-replacement` — ADD a new, correctly-named row copying qty/price/
# picture from an existing row, WITHOUT touching the old row. Built
# 2026-09-02 after this listing (336288009980) was found to reject
# ReviseItem AND ReviseFixedPriceItem outright for a pure in-place rename
# ("SKU is required... / Variation Specifics provided does not match" --
# a "fulfilled by eBay" validation, confirmed independent of SKU presence
# and independent of which Revise call is used). Testing whether an
# ADD-only revise (the same operation type push_single_card_live() already
# does successfully elsewhere) is accepted where a pure rename is not.
# ══════════════════════════════════════════════════════════════════════════════

def cmd_add_replacement(item_id: str, specific_name: str, old_value: str, new_value: str,
                         account_num: int = 1, dry_run: bool = True):
    item = fetch_item(item_id, account_num=account_num)
    variations = deep_copy_variations(item)

    specifics_set = get_specifics_set(variations)
    if new_value in specifics_set.get(specific_name, []):
        print(f"❌ Aborting — {new_value!r} already exists as a distinct value.")
        return

    # Read qty/price/pictures from the OLD row BEFORE normalize_quantities/
    # strip_selling_status touch anything, using the same helpers the
    # proven push/revise paths use (find_variation_by_specifics,
    # get_quantity_sold-equivalent via normalize_quantities).
    old_var = find_variation_by_specifics(variations, specific_name, old_value)
    if old_var is None:
        print(f"❌ Aborting — {old_value!r} not found live — can't copy qty/price from it.")
        return

    normalize_quantities(variations)  # old_var's <Quantity> is now the real available qty
    qty_el = _find(old_var, "Quantity")
    price_el = _find(old_var, "StartPrice")
    available_qty = int(qty_el.text) if qty_el is not None and qty_el.text else 0
    price = float(price_el.text) if price_el is not None and price_el.text else 0.0

    picture_urls = []
    pictures_node = _find(variations, "Pictures")
    if pictures_node is not None:
        for pic_set in _findall(pictures_node, "VariationSpecificPictureSet"):
            if _text(pic_set, "VariationSpecificValue") == old_value:
                picture_urls = [u.text.strip() for u in _findall(pic_set, "PictureURL") if u.text]

    print(f"Item {item_id}")
    print(f"Old row {old_value!r}: qty {available_qty}, price ${price:.2f}, {len(picture_urls)} picture(s)")
    print(f"Will ADD new row {new_value!r} with the same qty/price/picture(s). "
          f"Old row is left completely untouched — no rename, no delete.")

    if dry_run:
        print("\n(DRY RUN — no changes were made. Re-run with dry_run=False to apply.)")
        return

    strip_selling_status(variations)
    insert_specifics_value(variations, specific_name, new_value)
    add_variation_row(variations, {specific_name: new_value}, quantity=available_qty, start_price=price)
    if picture_urls:
        set_variation_picture(variations, new_value, picture_urls)

    revise_xml = build_revise_xml(item_id, variations, "ReviseFixedPriceItem", account_num=account_num)
    print("\nCalling ReviseFixedPriceItem...")
    _post("ReviseFixedPriceItem", revise_xml, account_num=account_num)
    print("✅ ReviseFixedPriceItem succeeded.")

    print("\nRe-fetching to verify...")
    new_item = fetch_item(item_id, account_num=account_num)
    new_summary = summarize(new_item)
    live_values = new_summary["specifics_set"].get(specific_name, [])
    if new_value in live_values:
        print(f"✅ {new_value!r} is now live.")
    else:
        print(f"⚠️  {new_value!r} NOT found live after the call — check manually.")
    if old_value in live_values:
        print(f"✅ Old row {old_value!r} is still present, untouched, as expected.")
    else:
        print(f"⚠️  Old row {old_value!r} unexpectedly missing.")


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

    p_bulk_preview = sub.add_parser("bulk-preview",
        help="Dry run — resync ALL of a listing's variation names to render_variation_name(), no API write")
    p_bulk_preview.add_argument("item_id")
    p_bulk_preview.add_argument("specific_name", help='e.g. "Card Name"')
    p_bulk_preview.add_argument("--only", help="Comma-separated card_number(s) to restrict the batch to, e.g. 95 or 1,108")

    p_bulk_apply = sub.add_parser("bulk-apply",
        help="Apply ALL renames from bulk-preview in ONE ReviseItem call (with confirmation)")
    p_bulk_apply.add_argument("item_id")
    p_bulk_apply.add_argument("specific_name", help='e.g. "Card Name"')
    p_bulk_apply.add_argument("--only", help="Comma-separated card_number(s) to restrict the batch to, e.g. 95 or 1,108")
    p_bulk_apply.add_argument("--assign-sku", action="store_true",
        help="Also set a SKU (= platform_listings.id) on every variation missing one — "
             "needed when eBay rejects ReviseItem with 'SKU is required in every variation'")
    p_bulk_apply.add_argument("--revise-item", action="store_true",
        help="Use the legacy ReviseItem call (auction-style listings) instead of the default "
             "ReviseFixedPriceItem — same call type every other revise path in this codebase uses")

    p_add_repl = sub.add_parser("add-replacement",
        help="ADD a new correctly-named row copying qty/price/picture from an existing row, "
             "WITHOUT touching the old row (dry run by default)")
    p_add_repl.add_argument("item_id")
    p_add_repl.add_argument("specific_name", help='e.g. "PokeCard"')
    p_add_repl.add_argument("old_value")
    p_add_repl.add_argument("new_value")
    p_add_repl.add_argument("--apply", action="store_true", help="Actually call ReviseFixedPriceItem (default: dry run)")

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
        elif args.command == "bulk-preview":
            only = args.only.split(",") if args.only else None
            cmd_bulk_preview(args.item_id, args.specific_name, only)
        elif args.command == "bulk-apply":
            only = args.only.split(",") if args.only else None
            cmd_bulk_apply(args.item_id, args.specific_name, only,
                            assign_sku=args.assign_sku, use_revise_item=args.revise_item)
        elif args.command == "add-replacement":
            cmd_add_replacement(args.item_id, args.specific_name, args.old_value, args.new_value,
                                 dry_run=not args.apply)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
