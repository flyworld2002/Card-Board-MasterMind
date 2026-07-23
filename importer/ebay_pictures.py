"""
importer/ebay_pictures.py — Upload a picture to eBay's own image hosting
(EPS, UploadSiteHostedPictures). Used to attach a photo to a specific
<VariationSpecificPictureSet> entry when a new card variation goes live
(see ebay_variations_xml.py's set_variation_picture(), and
docs/plans/listing-pricing-system.md's EPS-picture section).

Deliberately separate from the R2/card_master.image_url_own catalog
pipeline (utils/r2_storage.py, importer/image_upload.py) — this only
ever uploads to eBay's own hosting, never touches our catalog. eBay's
Trading API does not accept an arbitrary external URL for a
variation-specific picture, only its own EPS-hosted ones — there is no
way to skip this upload step and just reference a URL directly.

Core logic (multipart-POST an image to UploadSiteHostedPictures) proven
against a real live listing in the one-off upload_listing_a_images.py
script at the repo root; this module is the same approach promoted into
importer/ so the Listing Pricing System's push flow can reuse it.

Known deprecation: eBay Picture Services (EPS) is being retired
September 30, 2026 — this will need to migrate to the Media API before
then (see CLAUDE.md).
"""

import xml.etree.ElementTree as ET

import requests

from importer.ebay import _find, _findall, _text, NS
from importer.ebay_auth import get_trading_headers, get_user_token, TRADING_API_URL


def upload_picture_bytes(image_bytes: bytes, filename: str, account_num: int = 1) -> str:
    """Uploads raw image bytes to eBay's EPS via UploadSiteHostedPictures. Returns the eBay-hosted FullURL."""
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

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    content_type = "image/png" if ext == "png" else "image/jpeg"
    files = [
        ("XML Payload", (None, xml, "text/xml")),
        ("image", (filename, image_bytes, content_type)),
    ]
    resp = requests.post(TRADING_API_URL, headers=headers, files=files, timeout=60)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    ack = _text(root, "Ack")
    if ack not in ("Success", "Warning"):
        errors = _findall(root, "Errors")
        msgs = [_text(e, "LongMessage") or _text(e, "ShortMessage") for e in errors]
        raise RuntimeError(f"UploadSiteHostedPictures error: {'; '.join(filter(None, msgs))}")

    details = _find(root, "SiteHostedPictureDetails")
    return details.find(f"{{{NS}}}FullURL").text


def upload_picture_from_url(source_url: str, account_num: int = 1) -> str:
    """Downloads an image from `source_url`, then uploads it to EPS. Returns the eBay-hosted FullURL."""
    resp = requests.get(source_url, timeout=30)
    resp.raise_for_status()
    filename = source_url.rsplit("/", 1)[-1].split("?")[0] or "card.jpg"
    return upload_picture_bytes(resp.content, filename, account_num=account_num)
