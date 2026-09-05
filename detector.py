"""Image format detection via magic bytes (file header).

Reads the first few bytes of a file to determine its actual image format,
completely ignoring the file extension. Supports all common raster formats.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

# ISOBMFF ftyp brands for HEIC family
HEIC_BRANDS: set[bytes] = {
    b"heic", b"heix", b"hevc", b"hevx",
    b"heim", b"heis", b"hevm", b"hevs",
    b"mif1", b"msf1",
}

# ISOBMFF ftyp brands for AVIF family
AVIF_BRANDS: set[bytes] = {
    b"avif", b"avis", b"mif1",
}

# All formats this tool can handle
SUPPORTED_FORMATS: set[str] = {
    "jpeg", "png", "webp", "avif", "heic", "gif", "bmp", "tiff",
}


def detect_format_from_bytes(data: bytes) -> Optional[str]:
    """Detect image format from magic bytes in an in-memory buffer.

    This is the core detection logic. ``detect_format`` (file-path variant)
    delegates to this after reading the file header.

    Args:
        data: At least the first 32 bytes of the file (more is fine).

    Returns:
        Format string ('jpeg', 'png', 'webp', 'avif', 'heic', 'gif',
        'bmp', 'tiff') or ``None`` if the buffer is not a recognized image.
    """
    if len(data) < 4:
        return None

    # --- JPEG: FF D8 FF ---
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"

    # --- PNG: 89 50 4E 47 0D 0A 1A 0A ---
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"

    # --- GIF: GIF87a or GIF89a ---
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"

    # --- BMP: BM ---
    if data[:2] == b"BM":
        return "bmp"

    # --- TIFF: II 2A 00 (little-endian) or MM 00 2A (big-endian) ---
    if data[:4] in (b"II\x2a\x00", b"MM\x00\x2a"):
        return "tiff"

    # --- WebP: RIFF .... WEBP ---
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "webp"

    # --- HEIC / AVIF: ISOBMFF ftyp box at offset 4 ---
    # Box structure: [4-byte size] [ftyp] [4-byte major brand] [minor brand] [compat brands...]
    if data[4:8] == b"ftyp" and len(data) >= 12:
        brand = data[8:12]
        if brand in HEIC_BRANDS:
            return "heic"
        if brand in AVIF_BRANDS:
            return "avif"

    return None


def detect_format(file_path: Union[str, Path]) -> Optional[str]:
    """Detect image format by reading magic bytes from the file header.

    Args:
        file_path: Path to the file to inspect.

    Returns:
        Format string ('jpeg', 'png', 'webp', 'avif', 'heic', 'gif',
        'bmp', 'tiff') or ``None`` if the file is not a recognized image.
    """
    path = Path(file_path)
    try:
        with open(path, "rb") as f:
            header = f.read(32)
    except (OSError, IOError):
        return None

    return detect_format_from_bytes(header)


def is_supported(file_path: Union[str, Path]) -> bool:
    """Quick check: is this file a supported image format?"""
    return detect_format(file_path) in SUPPORTED_FORMATS
