"""
importer/ebay_variations_xml.py — Shared helpers for safely reading and
rewriting an eBay multi-variation listing's <Variations> block via the
Trading API (GetItem / ReviseItem / ReviseFixedPriceItem).

Extracted from importer/rename_variation.py's proven "deep-copy the whole
<Variations> block, mutate only what you mean to change, strip the
GetItem-only <SellingStatus> sub-element, resend byte-for-byte otherwise"
pattern (see that module's docstring for why this matters — a naive
partial revise risks eBay treating omitted parts as deletions). Used by
both rename_variation.py (renaming a VariationSpecifics value) and
importer/ebay_listing_sync.py (price/quantity push, variation add/remove
for the 250-cap holdback queue).

Every function here operates on a deep copy of the <Variations> element —
callers must fetch fresh via fetch_item() and deep-copy before mutating,
never mutate the node returned by fetch_item() directly.
"""

import copy
import xml.etree.ElementTree as ET

from importer.ebay import _post, _find, _findall, _text, NS
from importer.ebay_auth import get_user_token

EBAY_VARIATION_VALUE_MAX_LEN = 50


# ══════════════════════════════════════════════════════════════════════════════
# Fetch
# ══════════════════════════════════════════════════════════════════════════════

def fetch_item(item_id: str, account_num: int = 1) -> ET.Element:
    """GetItem with full detail, including Variations + Pictures. Returns <Item> element."""
    token = get_user_token(account_num=account_num)

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
  <IncludeItemSpecifics>true</IncludeItemSpecifics>
</GetItemRequest>"""

    root = _post("GetItem", xml, account_num=account_num)
    item = _find(root, "Item")
    if item is None:
        raise RuntimeError(f"GetItem returned no <Item> for {item_id}.")
    return item


def deep_copy_variations(item: ET.Element) -> ET.Element:
    """Deep-copy the <Variations> block from a fresh GetItem <Item> element."""
    variations_node = _find(item, "Variations")
    if variations_node is None:
        raise RuntimeError("Item has no <Variations> block.")
    return copy.deepcopy(variations_node)


def strip_selling_status(variations: ET.Element) -> None:
    """
    Remove <SellingStatus> from every <Variation> row, in place.
    GetItem-output-only; invalid on ReviseItem/ReviseFixedPriceItem input.
    """
    for var in _findall(variations, "Variation"):
        selling_status = _find(var, "SellingStatus")
        if selling_status is not None:
            var.remove(selling_status)


def get_quantity_sold(variation: ET.Element) -> int:
    """Read <SellingStatus><QuantitySold> from a freshly-fetched (not yet
    stripped) <Variation> row. eBay's <Quantity> is cumulative-ever-listed,
    not remaining stock — callers must add QuantitySold to the desired
    available quantity before writing <Quantity> (confirmed via live test,
    see docs/plans/ebay-listing-sync.md Step 0 #6)."""
    selling = _find(variation, "SellingStatus")
    if selling is None:
        return 0
    return int(_text(selling, "QuantitySold", "0") or 0)


# ══════════════════════════════════════════════════════════════════════════════
# Locating / mutating individual <Variation> rows
# ══════════════════════════════════════════════════════════════════════════════

def get_variation_specifics(variation: ET.Element) -> dict:
    """Returns {specific_name: value} for one <Variation> row."""
    specifics = {}
    specifics_node = _find(variation, "VariationSpecifics")
    if specifics_node is not None:
        for nvl in _findall(specifics_node, "NameValueList"):
            name = _text(nvl, "Name")
            val_el = _find(nvl, "Value")
            value = val_el.text.strip() if val_el is not None and val_el.text else None
            if name:
                specifics[name] = value
    return specifics


def find_variation_by_specifics(variations: ET.Element, specific_name: str, value: str):
    """Returns the first <Variation> element whose specifics[specific_name] == value, or None."""
    for var in _findall(variations, "Variation"):
        if get_variation_specifics(var).get(specific_name) == value:
            return var
    return None


def set_variation_price_qty(variation: ET.Element, start_price: float = None,
                             quantity: int = None) -> None:
    """Set <StartPrice> and/or <Quantity> on a <Variation> element, in place."""
    if start_price is not None:
        sp_el = _find(variation, "StartPrice")
        if sp_el is None:
            sp_el = ET.SubElement(variation, f"{{{NS}}}StartPrice")
        sp_el.text = f"{start_price:.2f}"

    if quantity is not None:
        qty_el = _find(variation, "Quantity")
        if qty_el is None:
            qty_el = ET.SubElement(variation, f"{{{NS}}}Quantity")
        qty_el.text = str(quantity)


def mark_variation_deleted(variation: ET.Element) -> None:
    """
    Flag a <Variation> row for deletion on the next Revise call
    (<Variation><Delete>true</Delete></Variation>). Confirmed live-viable
    even for a variation with past sales — see Step 0 #2 in
    docs/plans/ebay-listing-sync.md. Used for 250-cap promotion: delete a
    sold-out row to free a slot for the next queued card.
    """
    delete_el = _find(variation, "Delete")
    if delete_el is None:
        delete_el = ET.SubElement(variation, f"{{{NS}}}Delete")
    delete_el.text = "true"


def add_variation_row(variations: ET.Element, specifics: dict, quantity: int,
                       start_price: float, sku: str = None) -> ET.Element:
    """
    Append a new <Variation> row (for promoting a queued card into a freed
    slot). `specifics` is {specific_name: value} — every name used here
    must already exist as a NameValueList in VariationSpecificsSet (add it
    first via insert_specifics_value if it's a brand-new value).
    """
    var = ET.SubElement(variations, f"{{{NS}}}Variation")
    if sku:
        sku_el = ET.SubElement(var, f"{{{NS}}}SKU")
        sku_el.text = sku
    qty_el = ET.SubElement(var, f"{{{NS}}}Quantity")
    qty_el.text = str(quantity)
    sp_el = ET.SubElement(var, f"{{{NS}}}StartPrice")
    sp_el.text = f"{start_price:.2f}"

    specifics_node = ET.SubElement(var, f"{{{NS}}}VariationSpecifics")
    for name, value in specifics.items():
        nvl = ET.SubElement(specifics_node, f"{{{NS}}}NameValueList")
        name_el = ET.SubElement(nvl, f"{{{NS}}}Name")
        name_el.text = name
        val_el = ET.SubElement(nvl, f"{{{NS}}}Value")
        val_el.text = value

    return var


# ══════════════════════════════════════════════════════════════════════════════
# VariationSpecificsSet — the master {Name: [Values]} menu
# ══════════════════════════════════════════════════════════════════════════════

def get_specifics_set(variations: ET.Element) -> dict:
    """Returns {specific_name: [values, ...]} for VariationSpecificsSet."""
    result = {}
    specifics_set_node = _find(variations, "VariationSpecificsSet")
    if specifics_set_node is not None:
        for nvl in _findall(specifics_set_node, "NameValueList"):
            name = _text(nvl, "Name")
            values = [v.text.strip() for v in _findall(nvl, "Value") if v.text]
            if name:
                result[name] = values
    return result


def insert_specifics_value(variations: ET.Element, specific_name: str,
                            value: str, position: int = None) -> None:
    """
    Add `value` to VariationSpecificsSet's NameValueList for
    `specific_name`, at `position` (buyer-facing dropdown order follows
    this list's order — see the plan's "menu ordering matters" note for
    250-cap promotion). Appends at the end if position is None or out of
    range. No-ops if the value is already present. Existing values are
    NEVER removed (the menu is a permanent history of every value ever
    offered, per the locked design).
    """
    specifics_set_node = _find(variations, "VariationSpecificsSet")
    if specifics_set_node is None:
        specifics_set_node = ET.SubElement(variations, f"{{{NS}}}VariationSpecificsSet")

    target_nvl = None
    for nvl in _findall(specifics_set_node, "NameValueList"):
        if _text(nvl, "Name") == specific_name:
            target_nvl = nvl
            break

    if target_nvl is None:
        target_nvl = ET.SubElement(specifics_set_node, f"{{{NS}}}NameValueList")
        name_el = ET.SubElement(target_nvl, f"{{{NS}}}Name")
        name_el.text = specific_name

    existing_values = [v.text.strip() for v in _findall(target_nvl, "Value") if v.text]
    if value in existing_values:
        return

    new_val_el = ET.Element(f"{{{NS}}}Value")
    new_val_el.text = value

    value_elements = _findall(target_nvl, "Value")
    if position is None or position >= len(value_elements):
        target_nvl.append(new_val_el)
    else:
        anchor = value_elements[position]
        anchor_index = list(target_nvl).index(anchor)
        target_nvl.insert(anchor_index, new_val_el)


# ══════════════════════════════════════════════════════════════════════════════
# Building the Revise request
# ══════════════════════════════════════════════════════════════════════════════

def build_revise_xml(item_id: str, variations: ET.Element,
                      call_name: str = "ReviseFixedPriceItem",
                      account_num: int = 1) -> str:
    """
    Serialize a modified <Variations> subtree into a Revise*Item request.
    `call_name` must match whatever RequestName is passed to _post() —
    ReviseFixedPriceItem for fixed-price listings (the sync engine's case),
    ReviseItem for auction-style (rename_variation.py's case, kept as-is
    there for backward compatibility).
    """
    token = get_user_token(account_num=account_num)

    ET.register_namespace("", NS)
    variations_xml = ET.tostring(variations, encoding="unicode")

    return f"""<?xml version="1.0" encoding="utf-8"?>
<{call_name}Request xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{item_id}</ItemID>
    {variations_xml}
  </Item>
</{call_name}Request>"""
