"""Watermark asset management - loading, closing, cycling, and resize caching."""

import os
import threading
from typing import Callable, List, Optional

from PIL import Image

from core.services.watermark.common import (
    _RESAMPLE_LANCZOS,
    _safe_close,
    _wm_dbg,
)

# Try to import pillow_avif for AVIF support
try:
    import pillow_avif  # type: ignore
except Exception:
    pillow_avif = None


class WatermarkAssets:
    """Manages the lifecycle of watermark images.

    Handles loading from disk, thread-safe cyclic iteration,
    and Lanczos resize caching to avoid repeated expensive operations.
    """

    def __init__(self, debug_func: Optional[Callable[[str], None]] = None) -> None:
        self._watermarks_fullpage: List[Image.Image] = []
        self._watermarks_overlay: List[Image.Image] = []
        self._index_fullpage: int = 0
        self._index_overlay: int = 0
        self._index_lock = threading.Lock()
        self._resize_cache: dict[tuple, Image.Image] = {}
        self._resize_lock = threading.Lock()
        self._dbg = debug_func or _wm_dbg

    # ------------------------------------------------------------------
    # Properties (read-only access to loaded watermarks)
    # ------------------------------------------------------------------

    @property
    def watermarks_fullpage(self) -> List[Image.Image]:
        return self._watermarks_fullpage

    @property
    def watermarks_overlay(self) -> List[Image.Image]:
        return self._watermarks_overlay

    # ------------------------------------------------------------------
    # Load / Close
    # ------------------------------------------------------------------

    def load_watermarks(
        self, fullpage_paths: List[str], overlay_paths: List[str]
    ) -> bool:
        """Load watermark images from paths.

        Returns True if at least one watermark was loaded.
        """
        self._dbg(
            f"load_watermarks start: fullpage_paths={len(fullpage_paths)}, "
            f"overlay_paths={len(overlay_paths)}"
        )
        self.close_watermarks()

        for path in fullpage_paths:
            self._dbg(f"load fullpage watermark path='{path}'")
            if path and os.path.isfile(path):
                try:
                    wm = Image.open(path).convert("RGBA")
                    wm.load()
                    if wm.width <= 0 or wm.height <= 0:
                        raise ValueError("zero-sized image")
                    self._watermarks_fullpage.append(wm)
                    self._dbg(
                        f"loaded fullpage watermark: path='{path}', size={wm.size}, mode={wm.mode}"
                    )
                except (OSError, IOError, ValueError) as e:
                    print(f"Warning: Could not load fullpage watermark '{path}': {e}")
            else:
                self._dbg(f"fullpage watermark path skipped (invalid/missing): '{path}'")

        for path in overlay_paths:
            self._dbg(f"load overlay watermark path='{path}'")
            if path and os.path.isfile(path):
                try:
                    wm = Image.open(path).convert("RGBA")
                    wm.load()
                    if wm.width <= 0 or wm.height <= 0:
                        raise ValueError("zero-sized image")
                    self._watermarks_overlay.append(wm)
                    self._dbg(
                        f"loaded overlay watermark: path='{path}', size={wm.size}, mode={wm.mode}"
                    )
                except (OSError, IOError, ValueError) as e:
                    print(f"Warning: Could not load overlay watermark '{path}': {e}")
            else:
                self._dbg(f"overlay watermark path skipped (invalid/missing): '{path}'")

        loaded_any = bool(self._watermarks_fullpage or self._watermarks_overlay)
        self._dbg(
            f"load_watermarks done: fullpage_loaded={len(self._watermarks_fullpage)}, "
            f"overlay_loaded={len(self._watermarks_overlay)}, loaded_any={loaded_any}"
        )
        return loaded_any

    def close_watermarks(self) -> None:
        """Close all loaded watermark images to free memory."""
        self._dbg(
            f"close_watermarks: closing fullpage={len(self._watermarks_fullpage)}, "
            f"overlay={len(self._watermarks_overlay)}"
        )
        for wm in self._watermarks_fullpage + self._watermarks_overlay:
            _safe_close(wm)
        self._watermarks_fullpage = []
        self._watermarks_overlay = []
        self._index_fullpage = 0
        self._index_overlay = 0
        with self._resize_lock:
            for cached in self._resize_cache.values():
                _safe_close(cached)
            self._resize_cache.clear()

    # ------------------------------------------------------------------
    # Cyclic watermark selection (thread-safe)
    # ------------------------------------------------------------------

    def get_next_fullpage(self) -> Optional[Image.Image]:
        """Get next fullpage watermark in cyclic order."""
        with self._index_lock:
            if not self._watermarks_fullpage:
                self._dbg("get_next_fullpage: no watermark loaded")
                return None
            wm = self._watermarks_fullpage[self._index_fullpage]
            self._dbg(
                f"get_next_fullpage: index={self._index_fullpage}, size={wm.size}"
            )
            self._index_fullpage = (self._index_fullpage + 1) % len(
                self._watermarks_fullpage
            )
            return wm

    def get_next_overlay(self) -> Optional[Image.Image]:
        """Get next overlay watermark in cyclic order."""
        with self._index_lock:
            if not self._watermarks_overlay:
                return None
            wm = self._watermarks_overlay[self._index_overlay]
            self._index_overlay = (self._index_overlay + 1) % len(
                self._watermarks_overlay
            )
            return wm

    # ------------------------------------------------------------------
    # Resize cache (avoids expensive Lanczos on the same dimensions)
    # ------------------------------------------------------------------

    def get_resized_wm(
        self, watermark: Image.Image, target_w: int, target_h: int
    ) -> Image.Image:
        """Return resized watermark, using cache to avoid repeated Lanczos."""
        key = (id(watermark), target_w, target_h)
        cached = self._resize_cache.get(key)
        if cached is not None:
            return cached
        with self._resize_lock:
            if key not in self._resize_cache:
                self._resize_cache[key] = watermark.resize(
                    (target_w, target_h), _RESAMPLE_LANCZOS
                )
            return self._resize_cache[key]
