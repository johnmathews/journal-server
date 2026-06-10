"""HEIC → JPEG conversion round-trip against the real pillow-heif codec.

The api/ contract suites exercise `_convert_heic_to_jpeg` only through
mocked ingestion paths, so a pillow-heif major bump (0.22 → 1.x) could
break the actual decode without any test noticing. This module generates
a real HEIC image in-memory via pillow_heif's own encoder and pushes it
through the conversion helper, so the decoder/encoder pair is exercised
for real on every run.
"""

from __future__ import annotations

import io

import pillow_heif
import pytest
from PIL import Image

from journal.api._shared import _convert_heic_to_jpeg


def _make_heic_bytes(size: tuple[int, int] = (2, 2), color: str = "red") -> bytes:
    """Encode a tiny solid-color image as HEIC using pillow_heif itself."""
    pillow_heif.register_heif_opener()
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="HEIF")
    return buf.getvalue()


def test_convert_heic_to_jpeg_roundtrip() -> None:
    heic_data = _make_heic_bytes()

    jpeg_bytes, media_type = _convert_heic_to_jpeg(heic_data)

    assert media_type == "image/jpeg"
    out = Image.open(io.BytesIO(jpeg_bytes))
    assert out.format == "JPEG"
    assert out.size == (2, 2)


def test_convert_heic_preserves_dimensions() -> None:
    heic_data = _make_heic_bytes(size=(8, 4), color="blue")

    jpeg_bytes, _ = _convert_heic_to_jpeg(heic_data)

    out = Image.open(io.BytesIO(jpeg_bytes))
    assert out.size == (8, 4)


def test_convert_non_heic_bytes_raise() -> None:
    """Garbage bytes must raise (callers rely on the exception path)."""
    with pytest.raises(Exception):  # noqa: B017 — PIL raises UnidentifiedImageError
        _convert_heic_to_jpeg(b"not an image at all")
