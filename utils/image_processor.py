"""
utils/image_processor.py
Resizes and converts card images to WebP for efficient storage.

Requires: Pillow
  pip install Pillow
"""

import io
from pathlib import Path
from PIL import Image, ExifTags

# Target dimensions for card images (standard Pokemon card ratio ~1:1.4)
CARD_WIDTH  = 800
CARD_HEIGHT = 1117
WEBP_QUALITY = 85   # 0-100. 85 = excellent quality, ~10x smaller than phone JPG


def process_card_image(source: str | Path) -> tuple[bytes, str]:
    """
    Load an image file, auto-rotate (EXIF), resize to card dimensions,
    and convert to WebP.

    Args:
        source: Path to the image file (JPG, PNG, HEIC, etc.)

    Returns:
        Tuple of (webp_bytes, suggested_filename)
        webp_bytes: compressed WebP image as bytes, ready to upload
        suggested_filename: e.g. 'charizard.webp'
    """
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {source}")

    with Image.open(path) as img:
        # Auto-rotate based on EXIF orientation (phone photos are often rotated)
        img = _fix_orientation(img)

        # Convert to RGB (handles PNG with alpha, HEIC, etc.)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Resize to card dimensions, preserving aspect ratio with padding
        img = _fit_to_card(img)

        # Encode to WebP in memory
        buffer = io.BytesIO()
        img.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=6)
        buffer.seek(0)
        webp_bytes = buffer.read()

    original_size_kb = path.stat().st_size // 1024
    new_size_kb      = len(webp_bytes) // 1024
    print(f"  Image: {original_size_kb}KB → {new_size_kb}KB "
          f"({int((1 - new_size_kb/original_size_kb)*100)}% smaller)")

    suggested_name = path.stem + ".webp"
    return webp_bytes, suggested_name


def _fix_orientation(img: Image.Image) -> Image.Image:
    """Rotate image based on EXIF orientation tag (common on phone photos)."""
    try:
        exif = img._getexif()
        if not exif:
            return img
        orientation_key = next(
            k for k, v in ExifTags.TAGS.items() if v == "Orientation"
        )
        orientation = exif.get(orientation_key)
        rotations = {3: 180, 6: 270, 8: 90}
        if orientation in rotations:
            img = img.rotate(rotations[orientation], expand=True)
    except (AttributeError, StopIteration, TypeError):
        pass
    return img


def _fit_to_card(img: Image.Image) -> Image.Image:
    """
    Resize image to fit within CARD_WIDTH x CARD_HEIGHT,
    maintaining aspect ratio. Centers on white background.
    """
    img.thumbnail((CARD_WIDTH, CARD_HEIGHT), Image.LANCZOS)

    # If aspect ratio doesn't match exactly, pad with white
    if img.size != (CARD_WIDTH, CARD_HEIGHT):
        background = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), (255, 255, 255))
        offset_x = (CARD_WIDTH  - img.width)  // 2
        offset_y = (CARD_HEIGHT - img.height) // 2
        background.paste(img, (offset_x, offset_y))
        return background

    return img
