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

**Before any build_revise_xml() call that resends the FULL <Variations>
block** (not a single targeted mutation like set_variation_picture() for
one variation, which only ever adds/replaces its own entry) — call
assert_no_picture_loss(before, after) with a fresh pre-mutation snapshot
as `before`. See that function's docstring for the 2026-09-22 incident
this exists to catch: a full-tree resend silently dropped ~145 of 150
per-variation pictures despite following the "byte-for-byte" pattern
above.
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


def normalize_quantities(variations: ET.Element) -> None:
    """
    Rewrites every <Variation>'s <Quantity> to its true current available
    amount (Quantity - QuantitySold), in place. Callers MUST run this
    after deep_copy_variations() and BEFORE strip_selling_status() (needs
    to read QuantitySold, which that call removes) — and before applying
    any of this batch's own intentional quantity changes, so those aren't
    immediately re-normalized away.

    Why this exists (confirmed live, 2026-08): eBay's ReviseItem /
    ReviseFixedPriceItem folds each variation's CURRENT QuantitySold into
    whatever raw <Quantity> is present in the request — for EVERY
    <Variation> element sent, not just the one(s) whose value actually
    changed. Since this codebase's revise pattern is "deep-copy the whole
    <Variations> block, mutate only what you mean to change, resend the
    rest byte-for-byte" (see module docstring), every untouched
    variation's stale raw <Quantity> — which already has ITS old sold
    count baked in from a previous revise — gets eBay's sold-folding
    applied to it AGAIN on every single revise call, silently
    re-inflating every variation on the listing, not just the one(s)
    actually being pushed. Confirmed with an exact reproduction: four
    sequential single-variation revises, each resending the other three
    untouched, inflated their stored Quantity by exactly one extra
    QuantitySold per subsequent revise pass. Normalizing every
    variation's Quantity down to its true available count before every
    revise — touched or not — means eBay's own sold-folding reconstructs
    the SAME true value instead of compounding a new one on top.
    """
    for var in _findall(variations, "Variation"):
        sold = get_quantity_sold(var)
        if sold <= 0:
            continue
        qty_el = _find(var, "Quantity")
        current = int(qty_el.text) if qty_el is not None and qty_el.text else 0
        set_variation_price_qty(var, quantity=max(current - sold, 0))


def mark_variation_deleted(variations: ET.Element, variation: ET.Element,
                            specific_name: str, prune_menu: bool = True) -> None:
    """
    Flag a <Variation> row for deletion on the next Revise call
    (<Variation><Delete>true</Delete></Variation>), AND (when
    `prune_menu` is True, the default) remove its value from
    VariationSpecificsSet plus its matching
    <VariationSpecificPictureSet> entry under <Pictures> (if either
    exists) — without this second part, the menu grows by one on every
    single deletion with nothing ever shrinking it, which eventually
    blows eBay's 250 cap even while the live <Variation> count stays
    correctly at 250 the whole time. Confirmed live, 2026-10-01, on
    "SV4 - Paradox Rift Reverse Holo - Ultra Rare" (335101632502): enough
    250-cap sold-out swaps over time grew the menu to 296 entries with
    only 250 actually live, and eBay rejected the next push outright
    ("Variations number exceeds limit 250") — then rejected the first
    straightforward fix too ("used for pictures does not exist in
    variation specific set") until the matching stale picture entries
    were removed in the same pass. See prune_stale_variation_specifics.py
    for the one-time catch-up this fix prevents from recurring.

    `prune_menu=False` is for a caller whose `variations` has already had
    its whole <Pictures> block stripped out of the outgoing payload for
    an unrelated reason (remove_single_card_live()'s 2026-09-12 fix for
    large-listing "System error" 10007 on ReviseFixedPriceItem) — pruning
    the menu value there would leave eBay's OWN STORED Pictures data
    (never round-tripped, so never touched) pointing at a menu value this
    call just told eBay to delete, and whether eBay validates that
    consistency server-side even with <Pictures> omitted from the request
    is untested. Pass False there until that's verified live, rather than
    risk breaking a currently-working path to fix one that wasn't.

    Flagging the <Variation> itself for deletion is confirmed live-viable
    even for a variation with past sales — see Step 0 #2 in
    docs/plans/ebay-listing-sync.md. Used for 250-cap promotion: delete a
    sold-out row to free a slot for the next queued card.
    """
    delete_el = _find(variation, "Delete")
    if delete_el is None:
        delete_el = ET.SubElement(variation, f"{{{NS}}}Delete")
    delete_el.text = "true"

    if not prune_menu:
        return

    value = get_variation_specifics(variation).get(specific_name)
    if value is None:
        return

    specifics_set_node = _find(variations, "VariationSpecificsSet")
    if specifics_set_node is not None:
        for nvl in _findall(specifics_set_node, "NameValueList"):
            if _text(nvl, "Name") == specific_name:
                for v_el in list(_findall(nvl, "Value")):
                    if v_el.text and v_el.text.strip() == value:
                        nvl.remove(v_el)
                break

    pictures_node = _find(variations, "Pictures")
    if pictures_node is not None:
        for vsp in list(_findall(pictures_node, "VariationSpecificPictureSet")):
            if _text(vsp, "VariationSpecificValue") == value:
                pictures_node.remove(vsp)


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

def set_variation_picture(variations: ET.Element, specific_value: str, picture_urls: list[str]) -> None:
    """
    Add or update the <VariationSpecificPictureSet> entry (inside
    <Variations><Pictures>) for one VariationSpecificValue with an
    ORDERED list of <PictureURL> entries (front first, then any
    additional photos — see resolve_photo_urls() in
    importer/card_photos.py, migration 041). Every URL must already be
    eBay-hosted (EPS's UploadSiteHostedPictures FullURL, see
    importer/ebay_pictures.py) — eBay's Trading API does not accept an
    arbitrary external URL here, only its own hosted ones.

    Replaces every existing <PictureURL> for this variation with the new
    list on each call — same "restage overwrites" semantics the old
    single-URL version had, just generalized to N URLs, not additive
    across repeated calls. No-ops if picture_urls is empty (leaves
    whatever, if anything, was already there untouched).

    Creates <Pictures> (and its <VariationSpecificName>, inferred from
    VariationSpecificsSet) if the listing has no per-variation pictures
    at all yet.

    Callers MUST call this only AFTER every add_variation_row() call for
    the current batch has already happened — both functions append to
    `variations` in place, and <Pictures> needs to land after every
    <Variation> element, never in between, or the revised XML is
    malformed. (_do_promotions() in ebay_pushprices.py collects all
    promotions first, then applies pictures in one pass at the end, for
    exactly this reason.)
    """
    if not picture_urls:
        return

    pictures_node = _find(variations, "Pictures")
    if pictures_node is None:
        pictures_node = ET.SubElement(variations, f"{{{NS}}}Pictures")

    if _find(pictures_node, "VariationSpecificName") is None:
        specifics_set = get_specifics_set(variations)
        specific_name = next(iter(specifics_set), None)
        if specific_name:
            name_el = ET.SubElement(pictures_node, f"{{{NS}}}VariationSpecificName")
            name_el.text = specific_name

    target_vsp = None
    for vsp in _findall(pictures_node, "VariationSpecificPictureSet"):
        if _text(vsp, "VariationSpecificValue") == specific_value:
            target_vsp = vsp
            break

    if target_vsp is None:
        target_vsp = ET.SubElement(pictures_node, f"{{{NS}}}VariationSpecificPictureSet")
        val_el = ET.SubElement(target_vsp, f"{{{NS}}}VariationSpecificValue")
        val_el.text = specific_value
    else:
        for existing_url_el in _findall(target_vsp, "PictureURL"):
            target_vsp.remove(existing_url_el)

    for url in picture_urls:
        pic_url_el = ET.SubElement(target_vsp, f"{{{NS}}}PictureURL")
        pic_url_el.text = url


def count_variation_pictures(variations: ET.Element) -> int:
    """Number of <VariationSpecificPictureSet> entries currently in
    `variations` -- the cheap signal for assert_no_picture_loss()."""
    pics = _find(variations, "Pictures")
    return len(_findall(pics, "VariationSpecificPictureSet")) if pics is not None else 0


def assert_no_picture_loss(before: ET.Element, after: ET.Element) -> None:
    """
    Hard guard: raises RuntimeError if `after` (the tree about to be sent
    to build_revise_xml + _post) has FEWER <VariationSpecificPictureSet>
    entries than `before` (a fresh fetch_item()/deep_copy_variations()
    snapshot taken before any mutation). Call this immediately before
    every build_revise_xml() call that resends the FULL <Variations>
    block rather than a single targeted mutation (set_variation_picture()
    for one variation is exempt -- it only ever adds/replaces one entry,
    never touches the rest).

    Real incident, 2026-09-22: a one-off script reordered
    VariationSpecificsSet's value list on a 150-variation listing,
    resent the "unchanged" rest of the tree per this module's own
    byte-for-byte pattern, and eBay silently dropped ~145 of 150
    per-variation pictures anyway -- exact mechanism never fully pinned
    down (GetItem read back correctly both before and after a later,
    successful revise, so it isn't a simple fetch-side truncation), but
    the process gap was real: nothing checked the picture count was
    still intact before that revise went out. This closes that gap
    mechanically instead of relying on a human remembering to check.
    """
    before_count = count_variation_pictures(before)
    after_count = count_variation_pictures(after)
    if after_count < before_count:
        raise RuntimeError(
            f"Refusing to revise: <VariationSpecificPictureSet> count would drop "
            f"from {before_count} to {after_count}. This is the exact failure mode "
            f"that wiped 30th Celebration's pictures on 2026-09-22 -- investigate "
            f"before overriding. If this drop is actually intended (e.g. a variation "
            f"was deliberately removed), rebuild `before` from a fresh fetch after "
            f"confirming, don't bypass this check."
        )


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
