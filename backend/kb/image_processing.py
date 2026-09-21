"""
Image processing for uploaded Article/ProductShowcase photos.

Only process_photo_image is needed — the KB has no crop tool.

Without this, uploaded photos would reach storage completely unprocessed
— a modern phone photo (or a hi-res product screenshot) routinely runs
several MB, far larger than anything a KB article page needs to load
fast. This resizes and re-compresses every photo down to a sane size
before it's saved, applied uniformly regardless of upload path.
"""
import io

from PIL import Image, ImageOps
from django.core.files.base import ContentFile

# Long edge cap, in pixels. Generous for full-screen viewing on any
# device and for the AI photo-scan (Gemini downsamples internally
# anyway) — comfortably below the point where a phone photo's extra
# resolution buys anything but bytes.
MAX_DIMENSION = 2000
JPEG_QUALITY = 85


def process_photo_image(file_obj):
    """
    Given the raw uploaded file for a Photo.image field (before save),
    return a Django ContentFile: EXIF-rotated, resized to fit within
    MAX_DIMENSION on its longest edge, re-encoded as JPEG, with EXIF
    metadata (including GPS, if present) stripped in the process.

    Returns None if the file can't be parsed as an image — the caller
    should fall back to saving the original file untouched (Django's own
    ImageField validation will already have rejected anything that
    isn't a real image before this ever runs).
    """
    try:
        file_obj.seek(0)
        img = Image.open(file_obj)
        img.load()
    except Exception:
        return None

    # Respect the camera's rotation (EXIF orientation) before the save
    # below strips all metadata — otherwise sideways/upside-down photos
    # would get baked in permanently.
    img = ImageOps.exif_transpose(img)

    # Flatten anything with transparency (PNG screenshots, some HEIC
    # decode paths, palette images) onto white before JPEG re-encoding,
    # which has no alpha channel support.
    if img.mode in ('RGBA', 'LA', 'P'):
        rgba = img.convert('RGBA')
        background = Image.new('RGB', rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    elif img.mode != 'RGB':
        img = img.convert('RGB')

    if img.width > MAX_DIMENSION or img.height > MAX_DIMENSION:
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    buffer = io.BytesIO()
    img.save(buffer, format='JPEG', quality=JPEG_QUALITY, optimize=True)
    buffer.seek(0)

    return ContentFile(buffer.read(), name='photo.jpg')
