"""
importer/ebay_media.py — Upload a picture to eBay's image hosting via the
Commerce Media API (OAuth/REST), the replacement for the Trading API's
UploadSiteHostedPictures (EPS) call, which eBay retired -- confirmed live
2026-10-29/30 (eBay began returning 404 "Resource not found", then a bare
410 from Akamai with no application response at all, for calls on that
endpoint; the rest of the Trading API was unaffected, so this is EPS
specifically, not a platform-wide shutdown).

Confirmed end-to-end live (2026-10-31, listing 336853381498): an image
uploaded here, then referenced in a real ReviseFixedPriceItem's
VariationSpecificPictureSet, was accepted and went live -- eBay's Trading
API does accept Media-API-hosted URLs for per-variation pictures, so
nothing downstream of the upload step (set_variation_picture(),
push_card_photo_live(), the whole push pipeline) needs to change. Both
the uploaded image's own URL and the pre-existing EPS-hosted URLs this
account already has on file live on the same i.ebayimg.com domain.

Auth: OAuth 2.0 (sell.inventory scope), NOT Trading API's Auth'n'Auth --
reuses get_access_token() from ebay_finances.py (same refresh-token-based
minting, in-process cache per account_num) rather than building a second
OAuth client. If a token refresh ever fails here with a scope error, the
account's refresh token needs re-consenting with sell.inventory included
(see ALL_SCOPES in ebay_finances.py and rotate_ebay_token.py).

One eBay-documented quirk worth knowing: an uploaded image is temporary
(eBay's response includes an expirationDate ~30 days out) until it's
actually referenced in a live listing -- which every real caller here
does within the same push, so this has never been observed to matter in
practice, but it's why a URL from this module shouldn't be stored
unused for a long time before being attached to something live.
"""

import requests

from importer.ebay_finances import get_access_token

MEDIA_API_UPLOAD_URL = "https://apim.ebay.com/commerce/media/v1_beta/image/create_image_from_file"


def upload_picture_bytes(image_bytes: bytes, filename: str, account_num: int = 1) -> str:
    """Uploads raw image bytes to eBay via the Commerce Media API.
    Returns the eBay-hosted image URL (same i.ebayimg.com domain as the
    old EPS-hosted URLs, confirmed interchangeable with them for every
    existing caller -- see module docstring).

    Same name/signature/return type as the old (now-dead) EPS-based
    upload_picture_bytes() in ebay_pictures.py, which now just delegates
    here -- every existing caller (card_photos.py, ebay_pushprices.py)
    needed zero changes.
    """
    token = get_access_token(account_num)

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    content_type = "image/png" if ext == "png" else "image/jpeg"

    resp = requests.post(
        MEDIA_API_UPLOAD_URL,
        headers={"Authorization": f"Bearer {token}"},
        files={"image": (filename, image_bytes, content_type)},
        timeout=60,
    )

    if resp.status_code != 201:
        raise RuntimeError(
            f"Media API image upload failed ({resp.status_code}): {resp.text[:500]}"
        )

    data = resp.json()
    image_url = data.get("imageUrl")
    if image_url:
        return image_url

    # Observed elsewhere (not yet ourselves) as a valid alternate shape:
    # imageUrl absent from the create response body, with the real image
    # details instead at the URL in the Location header. Falling back to
    # a GET there rather than assuming imageUrl is always present.
    location = resp.headers.get("Location")
    if not location:
        raise RuntimeError(
            f"Media API upload returned 201 but no imageUrl in the body and no "
            f"Location header to follow: {resp.text[:500]}"
        )
    follow = requests.get(location, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    follow.raise_for_status()
    image_url = follow.json().get("imageUrl")
    if not image_url:
        raise RuntimeError(f"Media API upload's Location follow-up had no imageUrl: {follow.text[:500]}")
    return image_url


def upload_picture_from_url(source_url: str, account_num: int = 1) -> str:
    """Downloads an image from `source_url`, then uploads it via the Media API.
    Returns the eBay-hosted image URL. Same contract as the old EPS-based
    version in ebay_pictures.py."""
    resp = requests.get(source_url, timeout=30)
    resp.raise_for_status()
    filename = source_url.rsplit("/", 1)[-1].split("?")[0] or "card.jpg"
    return upload_picture_bytes(resp.content, filename, account_num=account_num)
