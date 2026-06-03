"""
utils/r2_storage.py
Handles image uploads to Cloudflare R2.

R2 is S3-compatible, so we use boto3 with a custom endpoint.

Requires: boto3
  pip install boto3

Setup — add these to your .env:
  R2_ACCOUNT_ID       = your Cloudflare account ID
  R2_ACCESS_KEY_ID    = R2 API token access key
  R2_SECRET_ACCESS_KEY= R2 API token secret key
  R2_BUCKET_NAME      = your bucket name (e.g. 'card-inventory')
  R2_PUBLIC_URL       = your bucket's public URL
                        e.g. https://pub-xxxx.r2.dev  (if public bucket enabled)
                        or a custom domain like https://images.yourstore.com

How to get R2 credentials:
  1. Cloudflare dashboard → R2 → Create bucket (name it e.g. 'card-inventory')
  2. R2 → Manage R2 API tokens → Create API token
     - Permissions: Object Read & Write
     - Scope: your bucket
  3. Copy Account ID, Access Key ID, Secret Access Key into .env
  4. In your bucket settings → enable 'Public access' to get R2_PUBLIC_URL
"""

import os
import boto3
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()


def _get_client():
    """Create and return a boto3 S3 client pointed at Cloudflare R2."""
    account_id = os.getenv("R2_ACCOUNT_ID")
    if not account_id:
        raise EnvironmentError(
            "R2_ACCOUNT_ID not set in .env — see utils/r2_storage.py for setup instructions."
        )
    return boto3.client(
        "s3",
        endpoint_url        = f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id   = os.getenv("R2_ACCESS_KEY_ID"),
        aws_secret_access_key = os.getenv("R2_SECRET_ACCESS_KEY"),
        config              = Config(signature_version="s3v4"),
        region_name         = "auto",
    )


def upload_card_image(image_bytes: bytes, filename: str,
                      card_id: str = None) -> str:
    """
    Upload WebP image bytes to R2 and return the public URL.

    Args:
        image_bytes: Compressed WebP bytes from image_processor.
        filename:    Suggested filename e.g. 'charizard.webp'
        card_id:     Optional card UUID — used to build a stable object key.

    Returns:
        Public URL to the uploaded image.
    """
    bucket     = os.getenv("R2_BUCKET_NAME")
    public_url = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

    if not bucket:
        raise EnvironmentError("R2_BUCKET_NAME not set in .env")
    if not public_url:
        raise EnvironmentError("R2_PUBLIC_URL not set in .env")

    # Build a stable, collision-free object key
    # e.g. cards/abc12345/charizard.webp
    if card_id:
        short_id = card_id.replace("-", "")[:8]
        object_key = f"cards/{short_id}/{filename}"
    else:
        object_key = f"cards/{filename}"

    client = _get_client()
    client.put_object(
        Bucket      = bucket,
        Key         = object_key,
        Body        = image_bytes,
        ContentType = "image/webp",
        CacheControl= "public, max-age=31536000",  # cache for 1 year (images don't change)
    )

    full_url = f"{public_url}/{object_key}"
    print(f"  Uploaded → {full_url}")
    return full_url


def delete_card_image(image_url: str) -> bool:
    """
    Delete an image from R2 by its public URL.
    Used when replacing an existing own photo.

    Returns True if deleted, False if not found.
    """
    bucket     = os.getenv("R2_BUCKET_NAME")
    public_url = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

    if not image_url.startswith(public_url):
        # Not our image (e.g. it's still the API stock image) — nothing to delete
        return False

    object_key = image_url[len(public_url):].lstrip("/")

    try:
        client = _get_client()
        client.delete_object(Bucket=bucket, Key=object_key)
        print(f"  Deleted old image: {object_key}")
        return True
    except Exception as e:
        print(f"  Warning: could not delete old image — {e}")
        return False
