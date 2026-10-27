"""
upload_listing_a_images.py — Downloads the official per-card art
(KD5B_EN_{card_number}-2x.png) and uploads each to eBay's own image hosting
(UploadSiteHostedPictures), attaching them to the matching variation on the
live "1-84 Holo & Blank" Pitch Black listing (item 336691917730).

Holo and ex cards are excluded (kept on whatever image they currently have)
— official art for those hasn't landed yet.

Not wired into main.py — one-off tool for this listing's image rollout.

Usage:
    python upload_listing_a_images.py
"""

import copy
import xml.etree.ElementTree as ET

import openpyxl
import requests

from importer.ebay import _find, _findall, _text, _post, NS
from importer.ebay_auth import get_trading_headers, get_user_token, TRADING_API_URL

EXCEL_PATH = r"C:\Users\Fei\.claude\uploads\3dd3f4ee-7042-4fce-91cf-63f4d472b4a0\0acf5eed-Pokemon_Pitch_Black_Card_List.xlsx"
SHEET_NAME = "1-84 Holo & Blank"
ITEM_ID = "336691917730"
ACCOUNT_NUM = 1

IMAGE_URL_TEMPLATE = "https://dz3we2x72f7ol.cloudfront.net/expansions/pitch-black/en-us/KD5B_EN_{n}-2x.png"


def load_display_names(exclude_holo_and_ex: bool = True) -> dict:
    """card_number -> Display Name, straight from the spreadsheet.

    Holo and ex cards are excluded by default — official art for those
    isn't ready yet, so they should keep whatever image they currently have.
    """
    wb = openpyxl.load_workbook(EXCEL_PATH, data_only=True)
    ws = wb[SHEET_NAME]
    mapping = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        card_num, display_name = r[0], r[4]
        if card_num is None or display_name is None:
            continue
        if exclude_holo_and_ex and (display_name.endswith(" ex") or display_name.endswith("Holo")):
            continue
        mapping[card_num] = display_name
    return mapping


def upload_picture_from_url(source_url: str, account_num: int = ACCOUNT_NUM) -> str:
    """Downloads an image and uploads it to eBay's EPS via UploadSiteHostedPictures. Returns the eBay-hosted FullURL."""
    resp = requests.get(source_url, timeout=30)
    resp.raise_for_status()
    image_bytes = resp.content

    token = get_user_token(account_num)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  <PictureSet>Supersize</PictureSet>
</UploadSiteHostedPicturesRequest>"""

    headers = get_trading_headers("UploadSiteHostedPictures", account_num=account_num)
    headers.pop("Content-Type", None)  # let requests set the multipart boundary

    ext = source_url.rsplit(".", 1)[-1].split("?")[0].lower()
    content_type = "image/png" if ext == "png" else "image/jpeg"
    files = [
        ("XML Payload", (None, xml, "text/xml")),
        ("image", (f"card.{ext}", image_bytes, content_type)),
    ]
    upload_resp = requests.post(TRADING_API_URL, headers=headers, files=files, timeout=60)
    upload_resp.raise_for_status()
    root = ET.fromstring(upload_resp.content)

    ack = _text(root, "Ack")
    if ack not in ("Success", "Warning"):
        errors = _findall(root, "Errors")
        msgs = [_text(e, "LongMessage") or _text(e, "ShortMessage") for e in errors]
        raise RuntimeError(f"UploadSiteHostedPictures error: {'; '.join(filter(None, msgs))}")

    details = _find(root, "SiteHostedPictureDetails")
    return details.find(f"{{{NS}}}FullURL").text


def fetch_item(item_id: str, account_num: int = ACCOUNT_NUM) -> ET.Element:
    token = get_user_token(account_num)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  <ItemID>{item_id}</ItemID>
  <DetailLevel>ReturnAll</DetailLevel>
</GetItemRequest>"""
    root = _post("GetItem", xml, account_num=account_num)
    item = _find(root, "Item")
    if item is None:
        raise RuntimeError(f"GetItem returned no <Item> for {item_id}.")
    return item


def build_revised_variations(item: ET.Element, url_by_display_name: dict) -> ET.Element:
    variations_node = _find(item, "Variations")
    if variations_node is None:
        raise RuntimeError("Item has no <Variations> block.")

    variations = copy.deepcopy(variations_node)

    for var in _findall(variations, "Variation"):
        selling_status = _find(var, "SellingStatus")
        if selling_status is not None:
            var.remove(selling_status)

    pictures_node = _find(variations, "Pictures")
    updated = 0
    for vsp in _findall(pictures_node, "VariationSpecificPictureSet"):
        value = _text(vsp, "VariationSpecificValue")
        if value in url_by_display_name:
            pic_url_el = _find(vsp, "PictureURL")
            pic_url_el.text = url_by_display_name[value]
            updated += 1

    print(f"Matched and updated {updated} of {len(url_by_display_name)} cards in the Pictures block.")
    return variations


def revise_item_pictures(item_id: str, variations: ET.Element, top_level_picture_url: str = None, account_num: int = ACCOUNT_NUM):
    token = get_user_token(account_num)
    root = ET.Element("ReviseItemRequest", xmlns=NS)
    creds = ET.SubElement(root, "RequesterCredentials")
    ET.SubElement(creds, "eBayAuthToken").text = token
    item = ET.SubElement(root, "Item")
    ET.SubElement(item, "ItemID").text = item_id
    if top_level_picture_url:
        pic_details = ET.SubElement(item, "PictureDetails")
        ET.SubElement(pic_details, "PictureURL").text = top_level_picture_url
    item.append(variations)
    xml_body = '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")

    result = _post("ReviseItem", xml_body, account_num=account_num)
    print("Ack:", _text(result, "Ack"))
    for err in _findall(result, "Errors"):
        print(f"  [{_text(err,'SeverityCode')} {_text(err,'ErrorCode')}] {_text(err,'LongMessage') or _text(err,'ShortMessage')}")


if __name__ == "__main__":
    display_names = load_display_names(exclude_holo_and_ex=True)
    print(f"Loaded {len(display_names)} card display names from the spreadsheet (Holo/ex excluded).\n")

    url_by_display_name = {}
    skipped = []
    for card_num in sorted(display_names):
        display_name = display_names[card_num]
        source_url = IMAGE_URL_TEMPLATE.format(n=card_num)
        print(f"[{card_num}/84] {display_name!r}")
        try:
            eps_url = upload_picture_from_url(source_url)
        except Exception as e:
            print(f"    SKIPPED: {e}")
            skipped.append(card_num)
            continue
        url_by_display_name[display_name] = eps_url
        print(f"    -> {eps_url}")

    if skipped:
        print(f"\nSkipped {len(skipped)} card(s) with no source image: {skipped}")

    print("\nFetching current listing and revising Variations.Pictures...")
    item = fetch_item(ITEM_ID)
    variations = build_revised_variations(item, url_by_display_name)
    revise_item_pictures(ITEM_ID, variations)
