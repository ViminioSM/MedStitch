"""Fullpage watermark detection and application engine."""

import random
from typing import Any, Callable, List, Optional, Tuple

import numpy as np
from PIL import Image

from core.services.watermark.assets import WatermarkAssets
from core.services.watermark.common import (
    Block,
    _safe_close,
    _wm_dbg,
    _WM_FULLPAGE_FAST_SELECT,
    _WM_FULLPAGE_INSERT_DEFAULT,
    WATERMARK_FULLPAGE_BLOCK_STRATEGY,
    WATERMARK_FULLPAGE_FREQUENCY,
    WATERMARK_FULLPAGE_POSITION,
)
from core.services.global_logger import logFunc


class FullpageEngine:
    """Fullpage watermark detection, positioning, and application."""

    def __init__(
        self,
        assets: WatermarkAssets,
        debug_func: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._assets = assets
        self._dbg = debug_func or _wm_dbg

    # ------------------------------------------------------------------
    # Uniform block detection
    # ------------------------------------------------------------------

    def find_uniform_blocks_fullpage(
        self,
        image: Image.Image,
        watermark: Image.Image,
        threshold: int = 150,
    ) -> List[Block]:
        """Find horizontal uniform blocks (near-white / near-black / quiet zones).

        *threshold* (0-255) controls tolerance. Lower = more lenient:
        - 100: white if row_min>=100, black if row_max<=155 — very tolerant
        - 150: white if row_min>=150, black if row_max<=105 — moderate
        - 200: white if row_min>=200, black if row_max<=55  — strict

        Also accepts "quiet zones": rows where pixel variance is low even if
        not strictly uniform. This catches gradients, sky backgrounds, etc.
        """
        wm_height = watermark.size[1]
        min_block_height = max(1, int(wm_height * 0.5))
        thr = max(0, min(255, int(threshold)))
        white_min = thr
        black_max = 255 - thr
        self._dbg(
            f"find_uniform_blocks_fullpage start: image_size={image.size}, "
            f"wm_size={watermark.size}, threshold={thr}, "
            f"min_block_height={min_block_height}"
        )
        grayscale = image.convert("L")
        img_array = np.array(grayscale, dtype=np.uint8)
        grayscale.close()

        # Strict uniform: entire row within white/black thresholds
        row_min = img_array.min(axis=1)
        row_max = img_array.max(axis=1)
        is_white_row = row_min >= white_min
        is_black_row = row_max <= black_max
        is_strict_uniform = is_white_row | is_black_row

        # Quiet zone: rows with low standard deviation (gradients, skies)
        row_std = np.std(img_array, axis=1, dtype=np.float64)
        is_quiet = (row_std < 30) & ~is_strict_uniform

        # Combine: strict uniform OR quiet zone
        is_uniform = is_strict_uniform | is_quiet

        padded = np.zeros(len(is_uniform) + 2, dtype=np.int8)
        padded[1:-1] = is_uniform.astype(np.int8)
        changes = np.diff(padded)
        run_starts = np.where(changes == 1)[0]
        run_ends = np.where(changes == -1)[0]

        blocks = []
        for start, end in zip(run_starts, run_ends, strict=False):
            block_h = int(end - start)
            if block_h >= min_block_height:
                color_is_white = bool(is_white_row[start]) if start < len(is_white_row) else True
                blocks.append((0, int(start), block_h, color_is_white))
                self._dbg(
                    f"accepted block: y={int(start)}, height={block_h}, "
                    f"type={'white' if color_is_white else 'black'}"
                )

        self._dbg(f"find_uniform_blocks_fullpage done: total_blocks={len(blocks)}")
        return blocks

    # ------------------------------------------------------------------
    # Positioning helpers
    # ------------------------------------------------------------------

    def calculate_watermark_position_in_block(
        self,
        block_y: int,
        block_height: int,
        wm_width: int,
        wm_height: int,
        position_type: int,
        image_width: int,
        spacing_top: int = 20,
        spacing_bottom: int = 20,
    ) -> Tuple[int, int]:
        """Calculate watermark position within block with configurable spacing."""
        if position_type == WATERMARK_FULLPAGE_POSITION.TOP:
            wm_y = block_y + spacing_top
        elif position_type == WATERMARK_FULLPAGE_POSITION.BOTTOM:
            wm_y = block_y + block_height - wm_height - spacing_bottom
        else:  # CENTER
            wm_y = block_y + (block_height - wm_height) // 2

        wm_x = (image_width - wm_width) // 2
        return wm_x, wm_y

    def _validate_block_has_space(
        self,
        block_height: int,
        wm_height: int,
        position_type: int,
        spacing_top: int,
        spacing_bottom: int,
        require_centered: bool,
    ) -> bool:
        """Validate if block has enough space for watermark with spacing."""
        if position_type == WATERMARK_FULLPAGE_POSITION.TOP:
            required = spacing_top + wm_height
            return block_height >= required

        elif position_type == WATERMARK_FULLPAGE_POSITION.BOTTOM:
            required = wm_height + spacing_bottom
            return block_height >= required

        else:  # CENTER
            required = spacing_top + wm_height + spacing_bottom
            return block_height >= required

    # ------------------------------------------------------------------
    # Fallback: insert at a fixed page position when no blocks found
    # ------------------------------------------------------------------

    def _insert_at_position(
        self,
        image: Image.Image,
        resized_watermark: Image.Image,
        insert_y: int,
    ) -> Optional[Image.Image]:
        """Insert watermark at specific y position (cut and expand)."""
        if insert_y < 0 or insert_y >= image.height:
            return None

        result = image.convert("RGBA")
        top_part = result.crop((0, 0, result.width, insert_y))
        bottom_part = result.crop((0, insert_y, result.width, result.height))

        wm_height = resized_watermark.size[1]
        new_height = result.height + wm_height
        new_image = Image.new("RGBA", (result.width, new_height), (255, 255, 255, 0))

        new_image.paste(top_part, (0, 0))
        new_image.paste(resized_watermark, (0, insert_y), resized_watermark)
        new_image.paste(bottom_part, (0, insert_y + wm_height))

        top_part.close()
        bottom_part.close()
        result.close()

        self._dbg(f"fallback insert at y={insert_y}, new_size={new_image.size}")
        return new_image

    # ------------------------------------------------------------------
    # Fullpage watermark application
    # ------------------------------------------------------------------

    @logFunc(inclass=True)
    def add_watermark_fullpage(
        self,
        image: Image.Image,
        settings: Optional[dict[str, Any]] = None,
    ) -> Optional[Image.Image]:
        """Apply fullpage watermark with positioning and frequency settings."""
        self._dbg("add_watermark_fullpage start")
        if not self._assets.watermarks_fullpage:
            self._dbg("add_watermark_fullpage aborted: no fullpage watermarks loaded")
            return None

        if settings is None:
            settings = {}

        try:
            position_type = int(
                settings.get(
                    "watermark_fullpage_position",
                    WATERMARK_FULLPAGE_POSITION.CENTER,
                )
            )
        except (TypeError, ValueError):
            position_type = int(WATERMARK_FULLPAGE_POSITION.CENTER)

        max_per_page = settings.get("watermark_fullpage_max_per_page", 1)

        try:
            frequency = int(
                settings.get(
                    "watermark_fullpage_frequency",
                    WATERMARK_FULLPAGE_FREQUENCY.ONCE_PER_PAGE,
                )
            )
        except (TypeError, ValueError):
            frequency = int(WATERMARK_FULLPAGE_FREQUENCY.ONCE_PER_PAGE)

        try:
            block_strategy = int(
                settings.get(
                    "watermark_fullpage_block_strategy",
                    WATERMARK_FULLPAGE_BLOCK_STRATEGY.BEST,
                )
            )
        except (TypeError, ValueError):
            block_strategy = int(WATERMARK_FULLPAGE_BLOCK_STRATEGY.BEST)

        try:
            alternate_interval = max(
                1, int(settings.get("watermark_fullpage_alternate_interval", 2))
            )
        except (TypeError, ValueError):
            alternate_interval = 2

        try:
            threshold = int(settings.get("watermark_fullpage_threshold", 150))
        except (TypeError, ValueError):
            threshold = 150
        threshold = max(50, min(255, threshold))

        spacing_top = settings.get("watermark_fullpage_min_spacing_top", 20)
        spacing_bottom = settings.get("watermark_fullpage_min_spacing_bottom", 20)
        require_centered = settings.get(
            "watermark_fullpage_require_centered_space", False
        )

        insert_mode = settings.get(
            "watermark_fullpage_insert_mode", _WM_FULLPAGE_INSERT_DEFAULT
        )
        min_area_height = settings.get("watermark_fullpage_min_area_height", 100)
        self._dbg(
            "add_watermark_fullpage settings: "
            f"position_type={position_type}, max_per_page={max_per_page}, "
            f"threshold={threshold}, "
            f"spacing_top={spacing_top}, spacing_bottom={spacing_bottom}, "
            f"require_centered={require_centered}, insert_mode={insert_mode}, "
            f"min_area_height={min_area_height}"
        )

        watermark = self._assets.get_next_fullpage()
        if watermark is None:
            self._dbg("add_watermark_fullpage aborted: get_next_fullpage returned None")
            return None
        if watermark.width <= 0 or watermark.height <= 0:
            self._dbg("add_watermark_fullpage aborted: zero-sized watermark asset")
            return None

        wm_width = image.width
        if wm_width <= 0:
            self._dbg(f"add_watermark_fullpage aborted: invalid wm_width={wm_width}")
            return None

        aspect_ratio = watermark.height / float(watermark.width)
        wm_height = int(wm_width * aspect_ratio)

        if wm_height > image.height - 3:
            wm_height = image.height - 3
            wm_width = int(wm_height / aspect_ratio) if aspect_ratio > 0 else 0

        if wm_width <= 0 or wm_height <= 0:
            self._dbg("add_watermark_fullpage aborted: invalid resized dimensions")
            return None

        resized_watermark = self._assets.get_resized_wm(watermark, wm_width, wm_height)
        self._dbg(f"fullpage watermark resized to {resized_watermark.size}")

        blocks = self.find_uniform_blocks_fullpage(
            image, resized_watermark, threshold=threshold
        )
        self._dbg(f"blocks detected={len(blocks)}")

        if max_per_page is None:
            max_per_page = 1

        try:
            max_per_page_int = int(max_per_page)
        except (TypeError, ValueError):
            max_per_page_int = 1

        if max_per_page_int <= 0:
            max_per_page_int = 1

        valid_blocks: List[Block] = []
        for block in blocks:
            block_x, block_y, block_height, is_white = block
            self._dbg(
                f"evaluate block: x={block_x}, y={block_y}, height={block_height}, "
                f"is_white={is_white}"
            )

            if block_height < min_area_height:
                self._dbg(
                    f"block rejected: height {block_height} < min_area_height {min_area_height}"
                )
                continue

            if not self._validate_block_has_space(
                block_height,
                wm_height,
                position_type,
                spacing_top,
                spacing_bottom,
                require_centered,
            ):
                self._dbg(
                    "block rejected: insufficient space "
                    f"(block_height={block_height}, wm_height={wm_height})"
                )
                continue

            valid_blocks.append(block)

        if frequency == int(WATERMARK_FULLPAGE_FREQUENCY.ALL_BLOCKS):
            max_per_page_int = max(max_per_page_int, len(valid_blocks))
        elif frequency == int(WATERMARK_FULLPAGE_FREQUENCY.ONCE_PER_PAGE):
            max_per_page_int = min(max_per_page_int, 1)

        ordered = list(valid_blocks)
        if block_strategy == int(WATERMARK_FULLPAGE_BLOCK_STRATEGY.BEST):
            ordered.sort(key=lambda b: b[2], reverse=True)
        elif block_strategy == int(WATERMARK_FULLPAGE_BLOCK_STRATEGY.RANDOM):
            if ordered:
                random.shuffle(ordered)
        elif (
            block_strategy == int(WATERMARK_FULLPAGE_BLOCK_STRATEGY.FIRST)
            and _WM_FULLPAGE_FAST_SELECT
        ):
            pass

        if frequency == int(WATERMARK_FULLPAGE_FREQUENCY.ALTERNATING):
            ordered = ordered[::alternate_interval]

        selected_blocks = ordered[:max_per_page_int]
        self._dbg(
            f"candidate_count={len(valid_blocks)}, selected_blocks={len(selected_blocks)}, "
            f"max_per_page_int={max_per_page_int}"
        )

        # ------------------------------------------------------------------
        # Insert mode with best block
        # ------------------------------------------------------------------
        if insert_mode and selected_blocks:
            result = image.convert("RGBA")
            inserted = False

            for block_x, block_y, block_height, _is_white_block in reversed(
                selected_blocks
            ):
                safe_top = block_y + spacing_top
                safe_bottom = block_y + block_height - spacing_bottom
                if safe_bottom <= safe_top:
                    continue
                insert_y = safe_top + ((safe_bottom - safe_top) // 2)

                top_part = result.crop((0, 0, result.width, insert_y))
                bottom_part = result.crop((0, insert_y, result.width, result.height))

                new_height = result.height + wm_height
                new_image = Image.new(
                    "RGBA", (result.width, new_height), (255, 255, 255, 0)
                )

                new_image.paste(top_part, (0, 0))
                new_image.paste(resized_watermark, (0, insert_y), resized_watermark)
                new_image.paste(bottom_part, (0, insert_y + wm_height))

                top_part.close()
                bottom_part.close()
                result.close()
                result = new_image
                inserted = True
                self._dbg(f"inserted at y={insert_y}")

            if inserted:
                return result
            _safe_close(result)

        # ------------------------------------------------------------------
        # Insert mode: fallback to page middle when no blocks found
        # ------------------------------------------------------------------
        if insert_mode:
            mid_y = image.height // 2
            result = self._insert_at_position(
                image, resized_watermark, mid_y
            )
            if result is not None:
                return result

        # ------------------------------------------------------------------
        # Overlay mode: paste watermark on selected blocks
        # ------------------------------------------------------------------
        if selected_blocks:
            watermark_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
            self._dbg("overlay-on-blocks mode")

            for _block_x, block_y, block_height, _is_white_block in selected_blocks:
                wm_x, wm_y = self.calculate_watermark_position_in_block(
                    block_y,
                    block_height,
                    wm_width,
                    wm_height,
                    position_type,
                    image.width,
                    spacing_top,
                    spacing_bottom,
                )

                wm_x = max(0, min(wm_x, image.width - wm_width))
                wm_y = max(0, min(wm_y, image.height - wm_height))

                watermark_layer.paste(
                    resized_watermark, (wm_x, wm_y), resized_watermark
                )

            result = Image.alpha_composite(image.convert("RGBA"), watermark_layer)
            watermark_layer.close()
            return result

        # ------------------------------------------------------------------
        # Overlay mode: fallback overlay at page middle
        # ------------------------------------------------------------------
        watermark_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        mid_y = (image.height - wm_height) // 2
        mid_x = (image.width - wm_width) // 2
        mid_y = max(0, min(mid_y, image.height - wm_height))
        mid_x = max(0, min(mid_x, image.width - wm_width))

        watermark_layer.paste(resized_watermark, (mid_x, mid_y), resized_watermark)
        result = Image.alpha_composite(image.convert("RGBA"), watermark_layer)
        watermark_layer.close()
        self._dbg(f"fallback overlay at center: ({mid_x}, {mid_y})")
        return result
