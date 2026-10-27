"""
create_pitch_black_listings.py — One-off tool to build and submit the two
Pokemon Pitch Black (ME05) eBay variation listings, modeled on existing live
listings 336600421354 ("1-84 Holo & Blank") and 336600660278 ("All Cards").

Not wired into main.py — this is a single-use listing creation tool, not a
recurring importer. Data source: the Pitch Black card list Excel file
(sheets "1-84 Holo & Blank" and "All Cards"). Quantity is hardcoded to 1 for
this initial test batch (see EXCEL_PATH rows have no Quantity column data).

Usage:
    python -m create_pitch_black_listings verify a
    python -m create_pitch_black_listings verify b
    python -m create_pitch_black_listings add a
    python -m create_pitch_black_listings add b --other-item-id 123456789012
    python -m create_pitch_black_listings revise-description a --other-item-id 123456789012
"""

import argparse
import sys
import xml.etree.ElementTree as ET

import openpyxl

from importer.ebay import _post, _find, _findall, _text
from importer.ebay_auth import get_user_token

NS = "urn:ebay:apis:eBLBaseComponents"
ET.register_namespace("", NS)

EXCEL_PATH = r"C:\Users\Fei\.claude\uploads\3dd3f4ee-7042-4fce-91cf-63f4d472b4a0\0acf5eed-Pokemon_Pitch_Black_Card_List.xlsx"

CATEGORY_ID = "183454"
CONDITION_ID = "4000"
CONDITION_DESCRIPTOR_NAME = "40001"   # "Card Condition" structured descriptor, confirmed against both live templates
CONDITION_DESCRIPTOR_VALUE = "400010"  # "Ungraded - Near mint or better"
COUNTRY = "US"
POSTAL_CODE = "11355"
LOCATION = "Flushing, New York"
DISPATCH_TIME_MAX = "1"
CURRENCY = "USD"
LISTING_DURATION = "GTC"
LISTING_TYPE = "FixedPriceItem"

SHIPPING_PROFILE_ID = "247814373016"
RETURN_PROFILE_ID = "245131890016"
PAYMENT_PROFILE_ID = "245131892016"

PLACEHOLDER_IMAGE = "https://d1i787aglh9bmb.cloudfront.net/assets/img/me-expansions/me05/logo/en-us/me05-logo-2x.png"

AXIS_NAME = "Pokecards"

DESC_A_TEMPLATE = """<font style="" size="4"><div style=""><font size="4" style="font-family: Arial;">Mega Evolution - ME05 Pitch Black</font></div><div id="ds_div" style="font-family: Arial;"><font style="font-family:Arial" size="4"><div><font size="4"></font><div style="font-size: 18.6667px;"><font size="4">Genuine Pokemon Cards&nbsp;<div>Take your pick in the drop down Menu <br></div><div><br></div><div><span style="font-size: 18.6667px;">All cards are NM - M (Pack Fresh) </span><font style="font-family:Arial" size="4"><font style="font-family:Arial" size="4"><font size="4"><span style="font-size: 18.6667px;"></span></font></font></font><span style="font-size: 18.6667px;"><br></span></div><div><span style="font-size: 18.6667px;">Get your playset<br></span></div><div><span style="font-size: 18.6667px;"><br></span></div></font><div><font size="4"><font style="font-family:Arial" size="4"><font style="font-family:Arial" size="4"><font size="4"><span style="font-size: 18.6667px;">Note: </span><font style="font-family:Arial" size="4"><font style="font-family:Arial" size="4"><font size="4"><span style="font-size: 18.6667px;">Images in the listing are sample pictures. <br></span></font></font></font></font></font></font></font></div><div><font style="font-family:Arial" size="4"><font style="font-family:Arial" size="4"><font size="4"><font style="font-family:Arial" size="4"><font style="font-family:Arial" size="4"><font size="4"><span style="font-size: 18.6667px;"><br></span></font></font></font></font></font></font></div>

<div><font><font><font><font size="5"><b>For Reverse Holo to Ultra Rare ME05 Pitch Black Cards are in the link below:</b></font></font></font></font></div><div><font><font><font><font size="5"><b></b></font></font></font></font></div>
<div><span style="font-size: 18.6667px;"><a href="{other_url}">Pokemon MEG Pitch Black Reverse Holo to Ultra Rare</a></span></div><div><span style="font-size: 18.6667px;"></span></div>

</div></div></font></div></font>"""

DESC_B_TEMPLATE = """<table style="font-family: &quot;Microsoft YaHei&quot;; letter-spacing: normal; orphans: 2; text-transform: none; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; text-decoration-thickness: initial; text-decoration-style: initial; text-decoration-color: initial; border-spacing: 0px; width: 945.556px;" align="center"><tbody><tr><td><div id="ds_div"><font><div><font size="4" face="Arial">Mega Evolution Pitch Black</font></div><div id="ds_div" style="font-family: Arial;"><font><font size="4"></font><div><font><span style="font-size: 18.6667px;">Genuine Pokemon Cards&nbsp;</span><div style="font-size: 18.6667px;">Take your pick in the drop down Menu&nbsp;</div><div style="font-size: 18.6667px;"><span style="font-size: 18.6667px;">All cards are NM - M (Pack Fresh)</span></div><div style="font-size: 18.6667px;"><span style="font-size: 18.6667px;"><br></span></div><div style="font-size: 18.6667px;">Cards with Front and back image are the exact cards you'll be getting.&nbsp;</div><div style="font-size: 18.6667px;"><span style="font-size: 18.6667px;"><br></span></div><div><span style="font-size: 18.6667px;">If you have any questions, (International &amp; Shipping Concerns) feel free to reach out. I'll do my part to well take care of you.&nbsp;</span></div><div><span style="font-size: 18.6667px;"><br></span></div><div><span style="font-size: 18.6667px;"><b>Clicked on the link below to bring you to other Mega Evolution Pitch Black Cards:</b></span></div><div><span style="font-size: 18.6667px;"><a href="{other_url}">Pokemon MEG Pitch Black Common Uncommon and Rare Holo cards</a></span></div><div><span style="font-size: 18.6667px;"><br></span></div>

<div><b><font size="5">Other Holo to Ultra Rare Sets you might be interested in: <br></font></b></div>
<div><a href="https://www.ebay.com/itm/335404313281"><span style="font-size: 18.6667px;"></span></a><a href="https://www.ebay.com/itm/364198382892">Pokemon S&amp;V Scarlet and Violet Base Set Holo to Ultra Rare</a></div>
<div><span style="font-size: 18.6667px;"><a href="https://www.ebay.com/itm/334903449758">Pokemon S&amp;V Paldea Evolved Holo to Ultra Rare</a></span></div>
<div><span style="font-size: 18.6667px;"><a href="https://www.ebay.com/itm/334985403072">Pokemon S&amp;V Obsidian Flames Holo to Ultra Rare</a></span></div>
<div><span style="font-size: 18.6667px;"><a href="https://www.ebay.com/itm/335042047707">Pokemon S&amp;V 151 Holo to Ultra Rare</a></span></div>
<div><span style="font-size: 18.6667px;"><a href="https://www.ebay.com/itm/335101632502">Pokemon S&amp;V Paradox Rift Holo to Ultra Rare</a></span></div>
<div><span style="font-size: 18.6667px;"><a href="https://www.ebay.com/itm/335234206383">Pokemon S&amp;V Paldean Fates Holo to Ultra Rare</a></span></div>
<div><span style="font-size: 18.6667px;"><a href="https://www.ebay.com/itm/335316826805">Pokemon S&amp;V Temporal Forces Holo to Ultra Rare</a></span></div>

<div><span style="font-size: 18.6667px;"><br></span></div><div><span style="font-size: 18.6667px;"><br></span></div><div><span style="font-size: 18.6667px;">Full Art, Alternate Art, Character Art, Gallery, Special Illustration Rare, Illustration Rare, Black Star Promo<br></span></div></font></div></font></div></font></div></td></tr></tbody></table>"""

LISTINGS = {
    "a": dict(
        sheet="1-84 Holo & Blank",
        title="Pokemon TCG Pitch Black Set Single Cards 1-084 YOU CHOOSE! Ready to Ship!",
        sku="VarSingles",
        item_specifics=[
            ("Game", "Pokémon TCG"),
            ("Set", "Mega Evolution - Pitch Black"),
            ("Language", "English"),
            ("Manufacturer", "Nintendo"),
            ("Year Manufactured", "2026"),
            ("Card Type", "pokemon"),
            ("Country/Region of Manufacture", "United States"),
            ("Country of Origin", "United States"),
        ],
        desc_template=DESC_A_TEMPLATE,
    ),
    "b": dict(
        sheet="All Cards",
        title="Pokemon ME05 PBL Pitch Black NM YouPICK! 1-120 Reverse Holo RH ex IR FA SIR HR",
        sku="VarSinglesHolo",
        item_specifics=[
            ("Attribute/MTG:Color", "Energy"),
            ("Set", "Mega Evolution - Pitch Black"),
            ("Card Condition", "Near Mint"),
            ("Year Manufactured", "2026"),
            ("Material", "Card Stock"),
            ("Age Level", "6+"),
            ("Game", "Pokémon TCG"),
            ("Language", "English"),
            ("Manufacturer", "Nintendo"),
            ("Card Type", "Pokémon"),
            ("Country/Region of Manufacture", "United States"),
            ("Finish", "Reverse Holo"),
            ("Country of Origin", "United States"),
        ],
        desc_template=DESC_B_TEMPLATE,
    ),
}


def load_rows(sheet_name: str) -> list[dict]:
    """Skips rows with no quantity or quantity == 0 — means the card isn't in stock."""
    wb = openpyxl.load_workbook(EXCEL_PATH, data_only=True)
    ws = wb[sheet_name]
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        card_num, name, base_number, holo, display_name, price, quantity = r
        if display_name is None or price is None or not quantity:
            continue
        rows.append({"display_name": display_name, "price": price, "quantity": int(quantity)})
    return rows


def sub(parent, tag_name, text=None):
    el = ET.SubElement(parent, tag_name)
    if text is not None:
        el.text = str(text)
    return el


def build_item_xml(listing_key: str, other_item_id: str | None) -> ET.Element:
    cfg = LISTINGS[listing_key]
    rows = load_rows(cfg["sheet"])
    if not rows:
        raise RuntimeError(f"No rows loaded for sheet {cfg['sheet']!r}")

    other_url = f"https://www.ebay.com/itm/{other_item_id}" if other_item_id else "https://www.ebay.com/ (TBD - other listing not yet created)"
    description = cfg["desc_template"].format(other_url=other_url)

    item = ET.Element("Item")
    sub(item, "Title", cfg["title"])
    sub(item, "Description", description)
    sub(item, "SKU", cfg["sku"])

    cat = sub(item, "PrimaryCategory")
    sub(cat, "CategoryID", CATEGORY_ID)

    sub(item, "ConditionID", CONDITION_ID)

    cond_descriptors = sub(item, "ConditionDescriptors")
    cond_descriptor = sub(cond_descriptors, "ConditionDescriptor")
    sub(cond_descriptor, "Name", CONDITION_DESCRIPTOR_NAME)
    sub(cond_descriptor, "Value", CONDITION_DESCRIPTOR_VALUE)

    specifics = sub(item, "ItemSpecifics")
    for name, value in cfg["item_specifics"]:
        nvl = sub(specifics, "NameValueList")
        sub(nvl, "Name", name)
        sub(nvl, "Value", value)

    pics = sub(item, "PictureDetails")
    sub(pics, "GalleryType", "Gallery")
    sub(pics, "PictureURL", PLACEHOLDER_IMAGE)

    sub(item, "Country", COUNTRY)
    sub(item, "Location", LOCATION)
    sub(item, "PostalCode", POSTAL_CODE)
    sub(item, "Currency", CURRENCY)
    sub(item, "DispatchTimeMax", DISPATCH_TIME_MAX)
    sub(item, "ListingDuration", LISTING_DURATION)
    sub(item, "ListingType", LISTING_TYPE)

    total_qty = sum(r["quantity"] for r in rows)
    min_price = min(r["price"] for r in rows)
    price_el = sub(item, "StartPrice", f"{min_price:.2f}")
    price_el.set("currencyID", CURRENCY)
    sub(item, "Quantity", total_qty)

    profiles = sub(item, "SellerProfiles")
    ssp = sub(profiles, "SellerShippingProfile")
    sub(ssp, "ShippingProfileID", SHIPPING_PROFILE_ID)
    srp = sub(profiles, "SellerReturnProfile")
    sub(srp, "ReturnProfileID", RETURN_PROFILE_ID)
    spp = sub(profiles, "SellerPaymentProfile")
    sub(spp, "PaymentProfileID", PAYMENT_PROFILE_ID)

    variations = sub(item, "Variations")
    vss = sub(variations, "VariationSpecificsSet")
    nvl = sub(vss, "NameValueList")
    sub(nvl, "Name", AXIS_NAME)
    for row in rows:
        sub(nvl, "Value", row["display_name"])

    for row in rows:
        var = sub(variations, "Variation")
        sp = sub(var, "StartPrice", f"{row['price']:.2f}")
        sp.set("currencyID", CURRENCY)
        sub(var, "Quantity", row["quantity"])
        vs = sub(var, "VariationSpecifics")
        vnvl = sub(vs, "NameValueList")
        sub(vnvl, "Name", AXIS_NAME)
        sub(vnvl, "Value", row["display_name"])

    var_pics = sub(variations, "Pictures")
    sub(var_pics, "VariationSpecificName", AXIS_NAME)
    for row in rows:
        vsp = sub(var_pics, "VariationSpecificPictureSet")
        sub(vsp, "VariationSpecificValue", row["display_name"])
        sub(vsp, "PictureURL", PLACEHOLDER_IMAGE)

    return item


def build_request(call_name: str, listing_key: str, other_item_id: str | None, account_num: int = 1) -> str:
    token = get_user_token(account_num)
    root = ET.Element(f"{call_name}Request", xmlns=NS)
    creds = sub(root, "RequesterCredentials")
    sub(creds, "eBayAuthToken", token)
    item = build_item_xml(listing_key, other_item_id)
    root.append(item)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def run_call(call_name: str, listing_key: str, other_item_id: str | None, account_num: int = 1):
    xml_body = build_request(call_name, listing_key, other_item_id, account_num)
    root = _post(call_name, xml_body, account_num=account_num)
    ack = _text(root, "Ack")
    print(f"Ack: {ack}")
    for err in _findall(root, "Errors"):
        severity = _text(err, "SeverityCode")
        code = _text(err, "ErrorCode")
        msg = _text(err, "LongMessage") or _text(err, "ShortMessage")
        print(f"  [{severity} {code}] {msg}")
    item_id = _text(root, "ItemID")
    if item_id:
        print(f"ItemID: {item_id}")
    fees = _find(root, "Fees")
    if fees is not None:
        for fee in _findall(fees, "Fee"):
            name = _text(fee, "Name")
            amt_el = _find(fee, "Fee")
            if amt_el is not None and amt_el.text and float(amt_el.text) > 0:
                print(f"  Fee: {name} = {amt_el.text}")
    return root


def revise_description(listing_key: str, item_id: str, other_item_id: str, account_num: int = 1):
    cfg = LISTINGS[listing_key]
    other_url = f"https://www.ebay.com/itm/{other_item_id}"
    description = cfg["desc_template"].format(other_url=other_url)

    token = get_user_token(account_num)
    root = ET.Element("ReviseItemRequest", xmlns=NS)
    creds = sub(root, "RequesterCredentials")
    sub(creds, "eBayAuthToken", token)
    item = sub(root, "Item")
    sub(item, "ItemID", item_id)
    sub(item, "Description", description)
    xml_body = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")

    result = _post("ReviseItem", xml_body, account_num=account_num)
    print("Ack:", _text(result, "Ack"))
    for err in _findall(result, "Errors"):
        print(f"  [{_text(err,'SeverityCode')} {_text(err,'ErrorCode')}] {_text(err,'LongMessage') or _text(err,'ShortMessage')}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub_parsers = parser.add_subparsers(dest="action", required=True)

    for action in ("verify", "add"):
        p = sub_parsers.add_parser(action)
        p.add_argument("listing", choices=["a", "b"])
        p.add_argument("--other-item-id", default=None)
        p.add_argument("--account", type=int, default=1)

    p = sub_parsers.add_parser("revise-description")
    p.add_argument("listing", choices=["a", "b"])
    p.add_argument("--item-id", required=True)
    p.add_argument("--other-item-id", required=True)
    p.add_argument("--account", type=int, default=1)

    args = parser.parse_args()

    if args.action == "verify":
        run_call("VerifyAddFixedPriceItem", args.listing, args.other_item_id, args.account)
    elif args.action == "add":
        run_call("AddFixedPriceItem", args.listing, args.other_item_id, args.account)
    elif args.action == "revise-description":
        revise_description(args.listing, args.item_id, args.other_item_id, args.account)
