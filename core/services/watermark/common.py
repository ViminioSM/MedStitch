"""Shared utilities, constants, and helpers for the watermark engine."""

import os
from typing import Any, List, Optional, Tuple

from PIL import Image

from core.utils.constants import (
    WATERMARK_FULLPAGE_BLOCK_STRATEGY,
    WATERMARK_FULLPAGE_FREQUENCY,
    WATERMARK_FULLPAGE_POSITION,
    WATERMARK_OVERLAY_POSITION,
)

# ---------------------------------------------------------------------------
# Environment-tuneable knobs (env vars respected at import time)
# ---------------------------------------------------------------------------

_WM_DEBUG_ENABLED = os.getenv("SMARTSTITCH_WM_DEBUG", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_WM_WORKERS_LIMIT = 32
_WM_WORKERS_DEFAULT = min(
    _WM_WORKERS_LIMIT,
    max(
        4,
        int(os.getenv("SMARTSTITCH_WATERMARK_WORKERS", str((os.cpu_count() or 8) * 2))),
    ),
)
_WM_FAST_SAVE = os.getenv(
    "SMARTSTITCH_WM_FAST_SAVE",
    os.getenv("SMARTSTITCH_FAST_SAVE", "0"),
).strip().lower() in {"1", "true", "yes", "on"}
_WM_JPEG_SUBSAMPLING = int(
    os.getenv("SMARTSTITCH_WM_JPEG_SUBSAMPLING", "2" if _WM_FAST_SAVE else "0")
)
_WM_WEBP_METHOD = int(
    os.getenv("SMARTSTITCH_WM_WEBP_METHOD", "0" if _WM_FAST_SAVE else "4")
)
_WM_PNG_COMPRESS_LEVEL = int(os.getenv("SMARTSTITCH_WM_PNG_COMPRESS_LEVEL", "0"))
_WM_FULLPAGE_FAST_SELECT = os.getenv(
    "SMARTSTITCH_WM_FULLPAGE_FAST_SELECT", "1"
).strip().lower() in {"1", "true", "yes", "on"}
_WM_FULLPAGE_INSERT_DEFAULT = os.getenv(
    "SMARTSTITCH_WM_FULLPAGE_INSERT",
    "0" if _WM_FAST_SAVE else "1",
).strip().lower() in {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# PIL Resampling constant (compatible with older Pillow)
# ---------------------------------------------------------------------------

if hasattr(Image, "Resampling"):
    _RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
else:
    _RESAMPLE_LANCZOS = Image.LANCZOS

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Block = Tuple[int, int, int, bool]  # (x, y, height, is_white)
Position = Tuple[int, int]  # (x, y)

# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def _wm_dbg(message: str) -> None:
    """Verbose debug logger for watermark diagnostics."""
    if _WM_DEBUG_ENABLED:
        print(f"[WM-DEBUG] {message}")


def _safe_close(*images: Image.Image) -> None:
    """Safely close PIL images without raising exceptions."""
    for img in images:
        if img is not None:
            try:
                img.close()
            except Exception:  # nosec B110
                pass


def _normalize_jpeg_quality(value: int) -> int:
    """Clamp JPEG quality into Pillow-safe bounds (1-100)."""
    return max(1, min(100, int(value)))


def save_result_image(
    image_path: str,
    image: Image.Image,
    *,
    lossy_quality: int,
    src_icc: Optional[bytes] = None,
    src_exif: Optional[bytes] = None,
) -> None:
    """Save result image, preserving quality parameters and metadata where supported."""
    ext = os.path.splitext(image_path)[1].lower()
    save_kwargs: dict[str, Any] = {}
    if src_icc:
        save_kwargs["icc_profile"] = src_icc
    if src_exif:
        save_kwargs["exif"] = src_exif

    if ext in (".jpg", ".jpeg"):
        image.save(
            image_path,
            quality=_normalize_jpeg_quality(lossy_quality),
            subsampling=max(0, min(2, _WM_JPEG_SUBSAMPLING)),
            optimize=False,
            **save_kwargs,
        )
    elif ext == ".avif":
        image.save(
            image_path,
            quality=_normalize_jpeg_quality(lossy_quality),
            lossless=False,
            **save_kwargs,
        )
    elif ext == ".webp":
        image.save(
            image_path,
            quality=_normalize_jpeg_quality(lossy_quality),
            method=max(0, min(6, _WM_WEBP_METHOD)),
            **save_kwargs,
        )
    elif ext == ".png":
        image.save(
            image_path,
            compress_level=max(0, min(9, _WM_PNG_COMPRESS_LEVEL)),
            **save_kwargs,
        )
    else:
        image.save(image_path, **save_kwargs)
