"""Pixel comparison detector for finding optimal slice locations."""
import os

import numpy as np
from PIL import Image as pil

from core.services.global_logger import logFunc

try:
    from smartstitch_native import detect_bytes as _native_detect_bytes
    try:
        from smartstitch_native import detect as _native_detect_ndarray
    except ImportError:
        _native_detect_ndarray = None
    _HAS_NATIVE = True
except ImportError:
    _native_detect_bytes = None
    _native_detect_ndarray = None
    _HAS_NATIVE = False

_SAFETY_BAND_PX = 30
_MIN_SCAN_STEP = 30


def _read_verify_smartcut(kwargs: dict) -> bool:
    if "verify_smartcut" in kwargs and kwargs["verify_smartcut"] is not None:
        return bool(kwargs["verify_smartcut"])
    return (os.getenv("SMARTSTITCH_VERIFY_SMARTCUT", "1") or "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _read_min_slice(kwargs: dict, split_height: int) -> int:
    if "min_slice" in kwargs and kwargs["min_slice"] is not None:
        try:
            return max(0, int(kwargs["min_slice"]))
        except (TypeError, ValueError):
            pass
    raw = (os.getenv("SMARTSTITCH_MIN_SLICE") or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return 800


class PixelComparisonDetector:
    """Detects slice locations using row-uniformity band detection.

    A cut is only accepted when a contiguous band of 30 rows is found where
    each row is near-uniform black or white (thresholds scale with sensitivity).
    """

    @staticmethod
    def _sensitivity_thresholds(sensitivity: int) -> tuple[float, int, int, float]:
        s = max(0, min(100, int(sensitivity))) / 100.0
        return (
            0.85 + 0.13 * s,
            int(round(60 - 30 * s)),
            int(round(180 + 45 * s)),
            25.0 - 17.0 * s,
        )

    @staticmethod
    def _build_perfect_row_mask(
        img_array: np.ndarray,
        ignorable_pixels: int,
        sensitivity: int,
    ) -> np.ndarray:
        width = img_array.shape[1]
        left = max(0, int(ignorable_pixels))
        right = width - left
        if right <= left:
            return np.zeros(img_array.shape[0], dtype=bool)

        ratio_thresh, black_max, white_min, std_max = (
            PixelComparisonDetector._sensitivity_thresholds(sensitivity)
        )

        active = img_array[:, left:right].astype(np.float32, copy=False)
        black_ratio = np.mean(active <= black_max, axis=1)
        white_ratio = np.mean(active >= white_min, axis=1)
        std_dev = np.std(active, axis=1)

        return ((black_ratio >= ratio_thresh) | (white_ratio >= ratio_thresh)) & (
            std_dev < std_max
        )

    @staticmethod
    def _collect_band_centers(perfect_rows: np.ndarray) -> np.ndarray:
        last_row = perfect_rows.shape[0]
        band = _SAFETY_BAND_PX
        if last_row < band:
            return np.empty(0, dtype=np.int32)

        as_i32 = perfect_rows.astype(np.int32, copy=False)
        cumsum = np.cumsum(as_i32, dtype=np.int32)
        window_sums = cumsum[band - 1 :] - np.concatenate(
            ([0], cumsum[: last_row - band])
        )
        valid_starts = np.flatnonzero(window_sums == band)
        if valid_starts.size == 0:
            return np.empty(0, dtype=np.int32)
        return (valid_starts + (band // 2)).astype(np.int32, copy=False)

    @staticmethod
    def _nearest_band_center(
        centers: np.ndarray,
        row: int,
        last_slice: int,
    ) -> int | None:
        if centers.size == 0:
            return None

        band = _SAFETY_BAND_PX
        lo = max(last_slice + 1, int(row) - (band - 1))
        hi = int(row) + (band - 1)

        idx = int(np.searchsorted(centers, row))
        candidates: list[int] = []
        if 0 <= idx < centers.size:
            candidates.append(int(centers[idx]))
        if idx > 0:
            candidates.append(int(centers[idx - 1]))
        if idx + 1 < centers.size:
            candidates.append(int(centers[idx + 1]))

        best_center = None
        best_distance = None
        for center in candidates:
            if center <= last_slice or center < lo or center > hi:
                continue
            distance = abs(center - row)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_center = center
        return best_center

    @logFunc(inclass=True)
    def run(
        self,
        combined_img: pil.Image,
        split_height: int,
        *,
        scan_step: int = 5,
        ignorable_pixels: int = 0,
        sensitivity: int = 90,
        **kwargs,
    ) -> list[int]:
        """Find optimal slice locations requiring a 30px contiguous safety band.

        Stage 2 (smartcut, default on): candidate bands containing vertical ink
        strokes, high-variance blocks or transition-dense rows are vetoed, and
        gutter-like candidates (panel frame at the band edge) win the tie-break.
        Disable with SMARTSTITCH_VERIFY_SMARTCUT=0 for the legacy exact behavior.
        """
        verify = _read_verify_smartcut(kwargs)
        min_slice = _read_min_slice(kwargs, split_height)
        gray = combined_img.convert('L')
        w, h = gray.size

        if _HAS_NATIVE:
            # Zero-copy fast path: share the grayscale buffer with Rust via
            # numpy (no `tobytes()` copy). Falls back to bytes on any error.
            if _native_detect_ndarray is not None:
                try:
                    img_array = np.asarray(gray)
                    if (
                        img_array.shape == (h, w)
                        and img_array.dtype == np.uint8
                        and img_array.flags["C_CONTIGUOUS"]
                    ):
                        result = _native_detect_ndarray(
                            img_array,
                            int(split_height or 1),
                            sensitivity=int(sensitivity),
                            ignorable_pixels=int(ignorable_pixels),
                            scan_step=int(scan_step),
                            verify_smartcut=bool(verify),
                            min_slice=int(min_slice),
                        )
                        return result.tolist()
                except Exception:
                    pass
            raw = gray.tobytes()
            result = _native_detect_bytes(
                raw, w, h, split_height,
                sensitivity=sensitivity,
                ignorable_pixels=ignorable_pixels,
                scan_step=scan_step,
                verify_smartcut=bool(verify),
                min_slice=int(min_slice),
            )
            return result.tolist()

        img_array = np.array(gray)
        last_row = img_array.shape[0]
        if last_row <= 0:
            return [0, 1]

        split_height = max(1, int(split_height or 1))
        scan_step = max(_MIN_SCAN_STEP, int(scan_step or _MIN_SCAN_STEP))

        perfect_rows = self._build_perfect_row_mask(
            img_array, ignorable_pixels=ignorable_pixels, sensitivity=sensitivity
        )
        band_centers = self._collect_band_centers(perfect_rows)
        ratio_thresh, black_max, white_min, std_max = self._sensitivity_thresholds(
            int(sensitivity)
        )
        left = max(0, int(ignorable_pixels))
        width = img_array.shape[1]

        def _verify_py(center: int) -> bool:
            if not verify:
                return True
            band = _SAFETY_BAND_PX
            half = band // 2
            y0 = max(0, int(center) - half)
            y1 = min(int(h), y0 + band)
            if y1 <= y0:
                return False
            if y1 - y0 < band:
                y0 = max(0, y1 - band)
            window = img_array[y0:y1, left:width].astype(np.int32)
            if window.size == 0:
                return False
            active_w = window.shape[1]
            # Veto V: vertical dark strokes crossing the band.
            dark = window <= int(black_max)
            col_dark = dark.sum(axis=0)
            need = max(1, int(window.shape[0] * 4 / 5))
            bad_limit = max(3, active_w // 500)
            if int((col_dark >= need).sum()) >= bad_limit:
                return False
            # Veto B: high-variance 64px blocks.
            var_block_max = (float(std_max) * 1.5) ** 2
            for bx in range(0, active_w, 64):
                block = window[:, bx:bx + 64].astype(np.float64)
                if block.size == 0:
                    continue
                var = float(block.var()) if block.size > 1 else 0.0
                if var >= var_block_max:
                    return False
            # Veto T: transition-dense rows.
            trans_limit = max(6, active_w // 100)
            rows = [y0, (y0 + y1) // 2, y1 - 1]
            trans_sum = 0
            for y in rows:
                yy = max(0, min(int(h) - 1, int(y)))
                line = img_array[yy, left:width].astype(np.int32)
                if line.size < 2:
                    continue
                trans_sum += int((np.abs(np.diff(line)) > 40).sum())
            if trans_sum / max(1, len(rows)) > trans_limit:
                return False
            return True

        def _gutter_py(center: int) -> int:
            if not verify:
                return 0
            band = _SAFETY_BAND_PX
            half = band // 2
            y0 = max(0, int(center) - half)
            y1 = max(0, min(int(h) - 1, int(center) + half))
            active_w = max(1, width - left)
            for dy in range(-5, 6):
                for ye in (y0 + dy, y1 + dy):
                    if ye < 0 or ye >= int(h):
                        continue
                    line = img_array[int(ye), left:width]
                    if float((line <= int(black_max)).mean()) > 0.3:
                        return 300
            return 0

        def _pick(row_pos: int, last_slice: int) -> int | None:
            band = _SAFETY_BAND_PX
            lo = max(last_slice + 1, int(row_pos) - (band - 1))
            hi = int(row_pos) + (band - 1)
            idx = int(np.searchsorted(band_centers, row_pos))
            best = None
            best_score = None
            for ci in (idx, idx - 1, idx + 1):
                if ci < 0 or ci >= int(band_centers.size):
                    continue
                c = int(band_centers[ci])
                if c <= last_slice or c < lo or c > hi:
                    continue
                if min_slice > 0 and c - last_slice < min_slice:
                    continue
                if not _verify_py(c):
                    continue
                score = abs(c - int(row_pos)) + abs(c - last_slice - int(split_height)) // 4
                score -= _gutter_py(c)
                if best_score is None or score < best_score:
                    best_score = score
                    best = c
            return best

        slice_locations = [0]
        row = split_height
        move_up = True

        while row < last_row:
            picked = _pick(int(row), int(slice_locations[-1]))
            if picked is not None:
                slice_locations.append(picked)
                row = picked + split_height
                move_up = True
                continue
            # Raw band exists but smartcut vetoed it: scan down past artwork.
            raw_hit = self._nearest_band_center(
                band_centers, int(row), int(slice_locations[-1])
            )
            if raw_hit is not None and verify:
                row = int(row) + int(scan_step)
                move_up = False
                continue

            if row - slice_locations[-1] <= 0.4 * split_height:
                row = slice_locations[-1] + split_height
                move_up = False

            if move_up:
                row -= scan_step
                if row <= slice_locations[-1]:
                    row = slice_locations[-1] + split_height
                    move_up = False
                continue

            row += scan_step

        remaining_height = last_row - slice_locations[-1]
        if remaining_height > 50:
            slice_locations.append(last_row)
        elif slice_locations[-1] != last_row:
            slice_locations[-1] = last_row

        if not slice_locations or slice_locations[0] != 0:
            slice_locations.insert(0, 0)

        end_row = max(1, int(last_row))
        if slice_locations[-1] != end_row:
            slice_locations.append(end_row)

        normalized: list[int] = []
        for point in slice_locations:
            p = max(0, min(int(point), end_row))
            if not normalized or p > normalized[-1]:
                normalized.append(p)

        if len(normalized) < 2:
            normalized = [0, end_row]

        return normalized
