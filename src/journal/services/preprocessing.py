"""Image preprocessing for OCR — auto-rotate, crop, downscale, contrast."""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

# Threshold for ink detection in the binarised image. Pixels in the
# inverted grayscale above this value are treated as ink. Typical
# handwriting on white paper lands well above 30; light pencil marks
# or scanner noise sit below it.
_INK_THRESHOLD = 30

# Padding (pixels) around the detected ink bounding box. Keeps a
# comfortable margin so the vision model sees context around the text.
_CROP_PADDING = 40

# Images larger than this (on the long edge) are downscaled. Vision
# APIs have optimal resolution ranges and larger images just cost more
# tokens for no accuracy gain. 1800px preserves plenty of detail for
# handwritten text.
_MAX_LONG_EDGE = 1800

# JPEG quality for the output image.
_JPEG_QUALITY = 92


def _auto_rotate(img: Image.Image) -> Image.Image:
    """Apply EXIF orientation tag and strip the tag from the result."""
    return ImageOps.exif_transpose(img) or img


def _auto_crop(
    img: Image.Image,
    padding: int = _CROP_PADDING,
    threshold: int = _INK_THRESHOLD,
) -> Image.Image:
    """Crop to the bounding box of ink on the page.

    Works on double-page spreads: ``getbbox()`` returns a single box
    around ALL ink, so text spanning both sides of a fold is preserved.
    Returns the image unchanged if no ink is detected (blank page).
    """
    gray = ImageOps.invert(img.convert("L"))
    binary = gray.point(lambda p: 255 if p > threshold else 0)
    bbox = binary.getbbox()
    if bbox is None:
        return img

    left, upper, right, lower = bbox
    w, h = img.size
    left = max(0, left - padding)
    upper = max(0, upper - padding)
    right = min(w, right + padding)
    lower = min(h, lower + padding)
    return img.crop((left, upper, right, lower))


def _downscale(img: Image.Image, max_long_edge: int = _MAX_LONG_EDGE) -> Image.Image:
    """Downscale so the longest edge is at most *max_long_edge* pixels."""
    long_edge = max(img.size)
    if long_edge <= max_long_edge:
        return img
    scale = max_long_edge / long_edge
    new_size = (round(img.width * scale), round(img.height * scale))
    return img.resize(new_size, Image.LANCZOS)


def _enhance_contrast(img: Image.Image) -> Image.Image:
    """Stretch histogram to full range, cutting the extreme 1 %."""
    return ImageOps.autocontrast(img, cutoff=1)


def preprocess_image(image_data: bytes, media_type: str) -> tuple[bytes, str]:
    """Preprocess a journal page image for OCR.

    Pipeline: auto-rotate → RGB → crop → downscale → contrast.
    Always returns JPEG bytes and ``"image/jpeg"`` media type.
    """
    img = Image.open(io.BytesIO(image_data))
    original_size = img.size
    img = _auto_rotate(img)
    img = img.convert("RGB")
    img = _auto_crop(img)
    img = _downscale(img)
    img = _enhance_contrast(img)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    log.info(
        "Preprocessed image: %dx%d → %dx%d (%d → %d bytes)",
        original_size[0], original_size[1],
        img.size[0], img.size[1],
        len(image_data), buf.tell(),
    )
    return buf.getvalue(), "image/jpeg"
