"""
importer/ebay_pictures.py — Upload a picture to eBay's own image hosting.
Used to attach a photo to a specific <VariationSpecificPictureSet> entry
when a new card variation goes live (see ebay_variations_xml.py's
set_variation_picture(), and docs/plans/listing-pricing-system.md's
EPS-picture section).

Deliberately separate from the R2/card_master.image_url_own catalog
pipeline (utils/r2_storage.py, importer/image_upload.py) — this only
ever uploads to eBay's own hosting, never touches our catalog. eBay's
Trading API does not accept an arbitrary external URL for a
variation-specific picture, only its own hosted ones — there is no way
to skip this upload step and just reference a URL directly.

EPS RETIRED (confirmed live 2026-10-29/30): the Trading API's
UploadSiteHostedPictures call this module used to hit directly now
404s/410s. Both functions here are now thin wrappers delegating to
importer/ebay_media.py's Commerce Media API (OAuth) implementation —
same names, same signatures, same return value (an eBay-hosted URL,
still the i.ebayimg.com domain), kept in this file purely so every
existing caller (card_photos.py, ebay_pushprices.py) needed zero
changes. See ebay_media.py's own docstring for the real upload logic
and the live-confirmed proof that a Media-API-hosted URL works fine in
a Trading API ReviseFixedPriceItem's VariationSpecificPictureSet.
"""

from importer.ebay_media import upload_picture_bytes, upload_picture_from_url

__all__ = ["upload_picture_bytes", "upload_picture_from_url"]
