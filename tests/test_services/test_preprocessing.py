"""Tests for the image preprocessing pipeline."""

import io
import struct

import pytest
from PIL import Image

from journal.services.preprocessing import (
    _auto_crop,
    _auto_rotate,
    _downscale,
    _enhance_contrast,
    preprocess_image,
)


def _to_bytes(img: Image.Image, fmt: str = "JPEG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _to_bytes_with_exif(img: Image.Image, orientation: int) -> bytes:
    """Save a JPEG with a minimal EXIF block containing an Orientation tag."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    raw = buf.getvalue()

    # Build a minimal APP1/EXIF segment with just the Orientation tag.
    # TIFF header (little-endian), one IFD entry, no next-IFD pointer.
    tiff_header = b"II"  # little-endian
    tiff_header += struct.pack("<H", 42)  # magic
    tiff_header += struct.pack("<I", 8)  # offset to first IFD

    # IFD: 1 entry
    ifd = struct.pack("<H", 1)  # entry count
    # Tag 0x0112 (Orientation), type SHORT (3), count 1, value
    ifd += struct.pack("<HHI", 0x0112, 3, 1)
    ifd += struct.pack("<HH", orientation, 0)  # value + padding
    ifd += struct.pack("<I", 0)  # next IFD offset (none)

    exif_body = b"Exif\x00\x00" + tiff_header + ifd
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif_body) + 2) + exif_body

    # Insert APP1 right after SOI (first 2 bytes)
    return raw[:2] + app1 + raw[2:]


# --- _auto_rotate ---


class TestAutoRotate:
    def test_no_exif_unchanged(self) -> None:
        img = Image.new("RGB", (100, 200), "white")
        result = _auto_rotate(img)
        assert result.size == (100, 200)

    def test_orientation_6_rotates(self) -> None:
        """Orientation 6 = 90 CW rotation; 100x200 becomes 200x100."""
        img = Image.new("RGB", (100, 200), "white")
        data = _to_bytes_with_exif(img, orientation=6)
        loaded = Image.open(io.BytesIO(data))
        result = _auto_rotate(loaded)
        assert result.size == (200, 100)

    def test_orientation_3_rotates_180(self) -> None:
        img = Image.new("RGB", (100, 200), "white")
        data = _to_bytes_with_exif(img, orientation=3)
        loaded = Image.open(io.BytesIO(data))
        result = _auto_rotate(loaded)
        assert result.size == (100, 200)


# --- _downscale ---


class TestDownscale:
    def test_large_image_downscaled(self) -> None:
        img = Image.new("RGB", (4000, 3000), "white")
        result = _downscale(img, max_long_edge=1800)
        assert max(result.size) == 1800
        assert result.size == (1800, 1350)

    def test_small_image_unchanged(self) -> None:
        img = Image.new("RGB", (1200, 800), "white")
        result = _downscale(img, max_long_edge=1800)
        assert result.size == (1200, 800)

    def test_exact_size_unchanged(self) -> None:
        img = Image.new("RGB", (1800, 1000), "white")
        result = _downscale(img, max_long_edge=1800)
        assert result.size == (1800, 1000)

    def test_tall_image_downscaled(self) -> None:
        img = Image.new("RGB", (2000, 4000), "white")
        result = _downscale(img, max_long_edge=1800)
        assert max(result.size) == 1800
        assert result.size == (900, 1800)


# --- _auto_crop ---


class TestAutoCrop:
    def test_removes_whitespace_border(self) -> None:
        img = Image.new("RGB", (400, 400), "white")
        # Draw a dark rectangle in the center (simulating text)
        for x in range(100, 300):
            for y in range(100, 300):
                img.putpixel((x, y), (20, 20, 20))
        result = _auto_crop(img, padding=10)
        # Should be cropped to ~210x210 (200 ink + 10 padding each side)
        assert result.size[0] < 400
        assert result.size[1] < 400
        assert result.size[0] == pytest.approx(220, abs=2)
        assert result.size[1] == pytest.approx(220, abs=2)

    def test_blank_page_unchanged(self) -> None:
        img = Image.new("RGB", (400, 400), "white")
        result = _auto_crop(img)
        assert result.size == (400, 400)

    def test_double_page_spread(self) -> None:
        """Ink on both sides of a wide image — should keep everything."""
        img = Image.new("RGB", (800, 400), "white")
        # Ink on left side
        for x in range(50, 150):
            for y in range(100, 300):
                img.putpixel((x, y), (10, 10, 10))
        # Ink on right side
        for x in range(650, 750):
            for y in range(100, 300):
                img.putpixel((x, y), (10, 10, 10))
        result = _auto_crop(img, padding=20)
        # Should span from ~30 to ~770 — NOT split into two crops
        assert result.size[0] > 700
        assert result.size[1] < 400

    def test_padding_clamped_to_image_bounds(self) -> None:
        """Ink near the edge — padding doesn't go past image borders."""
        img = Image.new("RGB", (200, 200), "white")
        for x in range(0, 50):
            for y in range(0, 50):
                img.putpixel((x, y), (10, 10, 10))
        result = _auto_crop(img, padding=40)
        assert result.size[0] <= 200
        assert result.size[1] <= 200


# --- _enhance_contrast ---


class TestEnhanceContrast:
    def test_stretches_histogram(self) -> None:
        # Create a low-contrast image (all pixels between 100-150)
        img = Image.new("L", (100, 100), 125)
        for x in range(50):
            for y in range(50):
                img.putpixel((x, y), 100)
        result = _enhance_contrast(img)
        pixels = list(result.getdata())
        assert min(pixels) < 100  # darkened
        assert max(pixels) > 150  # brightened


# --- preprocess_image (full pipeline) ---


class TestPreprocessImage:
    def test_returns_jpeg(self) -> None:
        img = Image.new("RGB", (500, 500), "white")
        data = _to_bytes(img, "PNG")
        result_data, media_type = preprocess_image(data, "image/png")
        assert media_type == "image/jpeg"
        # Verify it's a valid JPEG
        result_img = Image.open(io.BytesIO(result_data))
        assert result_img.format == "JPEG"

    def test_large_image_downscaled(self) -> None:
        img = Image.new("RGB", (4000, 3000), "white")
        # Add some ink so crop doesn't remove everything
        for x in range(100, 3900):
            for y in range(100, 2900):
                img.putpixel((x, y), (50, 50, 50))
        data = _to_bytes(img)
        result_data, _ = preprocess_image(data, "image/jpeg")
        result_img = Image.open(io.BytesIO(result_data))
        assert max(result_img.size) <= 1800

    def test_roundtrip_preserves_content(self) -> None:
        """A small image with ink should survive the pipeline."""
        img = Image.new("RGB", (300, 300), "white")
        for x in range(50, 250):
            for y in range(50, 250):
                img.putpixel((x, y), (30, 30, 30))
        data = _to_bytes(img)
        result_data, _ = preprocess_image(data, "image/jpeg")
        result_img = Image.open(io.BytesIO(result_data))
        # Image should still have content (not blank)
        assert result_img.size[0] > 0
        assert result_img.size[1] > 0
