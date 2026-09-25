"""Overlay watermark detection and application engine."""

from typing import Any, Callable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from core.services.watermark.assets import WatermarkAssets
from core.services.watermark.common import (
    WATERMARK_OVERLAY_POSITION,
    _safe_close,
    _wm_dbg,
)
from core.services.global_logger import logFunc


class OverlayEngine:
    """Overlay watermark spatial detection, positioning, and application."""

    def __init__(
        self,
        assets: WatermarkAssets,
        debug_func: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._assets = assets
        self._dbg = debug_func or _wm_dbg

    # ------------------------------------------------------------------
    # Bounding-box overlap check
    # ------------------------------------------------------------------

    @staticmethod
    def _boxes_overlap(
        a: Tuple[int, int, int, int],
        b: Tuple[int, int, int, int],
        *,
        min_iou: float = 0.15,
    ) -> bool:
        """True when axis-aligned boxes overlap enough to avoid stacking overlays."""
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        if iw == 0 or ih == 0:
            return False
        inter = iw * ih
        area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
        return (inter / area_a) >= min_iou

    # ------------------------------------------------------------------
    # Spatial search for suitable overlay position
    # ------------------------------------------------------------------

    @logFunc(inclass=True)
    def find_suitable_space_overlay(
        self,
        image: Image.Image,
        watermark: Image.Image,
        threshold_white: int = 250,
        threshold_black: int = 30,
        contrast_threshold: int = 50,
        exclude_boxes: Optional[Sequence[Tuple[int, int, int, int]]] = None,
    ) -> Optional[Tuple[int, int]]:
        """Find suitable space for overlay watermark with optimized detection."""
        width, height = image.size
        wm_width, wm_height = watermark.size
        if wm_width <= 0 or wm_height <= 0:
            return None
        if wm_width > width or wm_height > height:
            return (0, 0)

        exclude = list(exclude_boxes or ())

        def _clamped_fallback() -> Tuple[int, int]:
            candidates = [
                (max(0, min(50, width - wm_width)), max(0, min(50, height - wm_height))),
                (max(0, width - wm_width - 50), max(0, min(50, height - wm_height))),
                (max(0, min(50, width - wm_width)), max(0, height - wm_height - 50)),
                (max(0, width - wm_width - 50), max(0, height - wm_height - 50)),
                ((width - wm_width) // 2, (height - wm_height) // 2),
            ]
            for cx, cy in candidates:
                box = (cx, cy, cx + wm_width, cy + wm_height)
                if not any(self._boxes_overlap(box, ex) for ex in exclude):
                    return (cx, cy)
            return candidates[0]

        grayscale = image.convert("L")
        try:
            img_array = np.asarray(grayscale, dtype=np.float32)
        finally:
            grayscale.close()

        full_mean = float(img_array.mean())
        full_stddev = float(img_array.std())

        if full_stddev < 5:
            return _clamped_fallback()

        adapted_white = threshold_white
        adapted_black = threshold_black
        adapted_contrast = contrast_threshold

        if full_mean > 200:
            adapted_white = min(threshold_white + 10, 255)
            adapted_contrast = contrast_threshold + 10
        elif full_mean < 100:
            adapted_black = max(threshold_black - 10, 10)
            adapted_contrast = contrast_threshold - 10

        integral = np.pad(
            img_array.astype(np.float64), ((1, 0), (1, 0)), mode="constant"
        )
        integral = np.cumsum(np.cumsum(integral, axis=0), axis=1)
        sq = np.pad(
            np.square(img_array, dtype=np.float64), ((1, 0), (1, 0)), mode="constant"
        )
        integral_sq = np.cumsum(np.cumsum(sq, axis=0), axis=1)

        area = float(wm_width * wm_height)
        step_size = max(8, min(wm_width // 4, max(1, wm_height // 2)))

        best_score = -1
        best_position: Optional[Tuple[int, int]] = None
        center_x = width // 2
        center_y = height // 2
        max_dist = ((width // 2) ** 2 + (height // 2) ** 2) ** 0.5 or 1.0

        max_y = height - wm_height
        max_x = width - wm_width
        for y in range(0, max_y + 1, step_size):
            y2 = y + wm_height
            for x in range(0, max_x + 1, step_size):
                x2 = x + wm_width
                box = (x, y, x2, y2)
                if exclude and any(self._boxes_overlap(box, ex) for ex in exclude):
                    continue
                region_sum = (
                    integral[y2, x2]
                    - integral[y, x2]
                    - integral[y2, x]
                    + integral[y, x]
                )
                region_sq = (
                    integral_sq[y2, x2]
                    - integral_sq[y, x2]
                    - integral_sq[y2, x]
                    + integral_sq[y, x]
                )
                mean = region_sum / area
                variance = max(0.0, (region_sq / area) - (mean * mean))
                stddev = variance**0.5

                score = 0
                if adapted_black < mean < adapted_white:
                    score += 50
                if 15 < stddev < adapted_contrast:
                    score += 30
                elif stddev <= 15:
                    score += 20

                dist_from_center = ((x - center_x) ** 2 + (y - center_y) ** 2) ** 0.5
                score += int(20 * (1 - dist_from_center / max_dist))

                if score > 40 and score > best_score:
                    best_score = score
                    best_position = (x, y)

        if best_position is not None:
            return best_position
        return _clamped_fallback()

    # ------------------------------------------------------------------
    # Overlay watermark application
    # ------------------------------------------------------------------

    @logFunc(inclass=True)
    def add_watermark_overlay(
        self,
        image: Image.Image,
        settings: Optional[dict[str, Any]] = None,
    ) -> Optional[Image.Image]:
        """Apply overlay watermark - smaller, with transparency."""
        if not self._assets.watermarks_overlay:
            return None

        if settings is None:
            settings = {}

        position_type = settings.get(
            "watermark_overlay_position", WATERMARK_OVERLAY_POSITION.AUTO
        )
        opacity = settings.get("watermark_overlay_opacity", 80)
        scale_pct = settings.get("watermark_overlay_scale_pct", 50)
        max_per_page = settings.get("watermark_overlay_max_per_page", 1)
        margin = settings.get("watermark_overlay_margin", 10)

        try:
            opacity_f = max(0.0, min(100.0, float(opacity))) / 100.0
        except Exception:
            opacity_f = 0.8

        try:
            scale_f = max(0.05, min(1.0, float(scale_pct) / 100.0))
        except Exception:
            scale_f = 0.5

        try:
            max_int = int(max_per_page)
        except Exception:
            max_int = 1

        if max_int <= 0:
            return None

        opacity_lut = [int(p * opacity_f) for p in range(256)]

        base = image.convert("RGBA") if image.mode != "RGBA" else image.copy()
        modified = False
        used_boxes: List[Tuple[int, int, int, int]] = []

        for _ in range(max_int):
            watermark = self._assets.get_next_overlay()
            if watermark is None:
                break
            if watermark.width <= 0 or watermark.height <= 0:
                continue

            wm_width = int(base.width * scale_f)
            if wm_width <= 0:
                continue
            aspect_ratio = watermark.height / float(watermark.width)
            wm_height = int(wm_width * aspect_ratio)
            if wm_height <= 0:
                continue
            if wm_width > base.width:
                wm_width = base.width
                wm_height = int(wm_width * aspect_ratio)
            if wm_height > base.height:
                wm_height = base.height
                wm_width = int(wm_height / aspect_ratio) if aspect_ratio > 0 else 0
            if wm_width <= 0 or wm_height <= 0:
                continue

            resized_watermark = self._assets.get_resized_wm(watermark, wm_width, wm_height)

            position = None
            if position_type == WATERMARK_OVERLAY_POSITION.AUTO:
                position = self.find_suitable_space_overlay(
                    base, resized_watermark, exclude_boxes=used_boxes
                )
            else:
                max_x = max(margin, base.width - wm_width - margin)
                max_y = max(margin, base.height - wm_height - margin)

                if position_type == WATERMARK_OVERLAY_POSITION.TOP_LEFT:
                    position = (margin, margin)
                elif position_type == WATERMARK_OVERLAY_POSITION.TOP_RIGHT:
                    position = (max_x, margin)
                elif position_type == WATERMARK_OVERLAY_POSITION.BOTTOM_LEFT:
                    position = (margin, max_y)
                elif position_type == WATERMARK_OVERLAY_POSITION.BOTTOM_RIGHT:
                    position = (max_x, max_y)
                elif position_type == WATERMARK_OVERLAY_POSITION.CENTER:
                    position = (
                        (base.width - wm_width) // 2,
                        (base.height - wm_height) // 2,
                    )
                else:
                    position = (margin, margin)

            if not position:
                break

            px, py = int(position[0]), int(position[1])
            px = max(0, min(px, base.width - wm_width))
            py = max(0, min(py, base.height - wm_height))
            box = (px, py, px + wm_width, py + wm_height)

            wm = resized_watermark.copy()
            alpha = wm.split()[3]
            alpha = alpha.point(opacity_lut)
            wm.putalpha(alpha)

            watermark_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
            watermark_layer.paste(wm, (px, py), wm)
            merged = Image.alpha_composite(base, watermark_layer)

            base.close()
            base = merged
            modified = True
            used_boxes.append(box)

            wm.close()
            watermark_layer.close()

        if not modified:
            base.close()
            return None

        return base
