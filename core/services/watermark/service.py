"""WatermarkService orchestrator for processing chapter folders."""

import concurrent.futures
import gc
import os
from typing import Any, Callable, List, Optional

from natsort import natsorted
from PIL import Image

from core.services.watermark.assets import WatermarkAssets
from core.services.watermark.common import (
    _WM_WORKERS_DEFAULT,
    _WM_WORKERS_LIMIT,
    _safe_close,
    _wm_dbg,
    save_result_image,
)
from core.services.watermark.fullpage import FullpageEngine
from core.services.watermark.header_footer import compose_with_header_footer
from core.services.watermark.overlay import OverlayEngine

# Try to import pillow_avif for AVIF support
try:
    import pillow_avif  # type: ignore
except Exception:
    pillow_avif = None


class WatermarkService:
    """High-level orchestrator that processes chapter folders with watermarks.

    Composes the asset manager, fullpage engine, overlay engine,
    and header/footer composition into a single workflow.
    """

    def __init__(self) -> None:
        self._assets = WatermarkAssets(debug_func=_wm_dbg)
        self._fullpage = FullpageEngine(self._assets, debug_func=_wm_dbg)
        self._overlay = OverlayEngine(self._assets, debug_func=_wm_dbg)
        self._last_run_info: dict[str, int | bool] = {
            "requested_workers": 0,
            "used_workers": 0,
            "parallel": False,
            "total_images": 0,
        }

    # ------------------------------------------------------------------
    # Properties (backward-compatible access to loaded watermarks)
    # ------------------------------------------------------------------

    @property
    def watermarks_fullpage(self) -> List[Image.Image]:
        return self._assets.watermarks_fullpage

    @property
    def watermarks_overlay(self) -> List[Image.Image]:
        return self._assets.watermarks_overlay

    @property
    def last_run_info(self) -> dict[str, int | bool]:
        return dict(self._last_run_info)

    # ------------------------------------------------------------------
    # Load / Close (delegate to assets)
    # ------------------------------------------------------------------

    def load_watermarks(
        self, fullpage_paths: List[str], overlay_paths: List[str]
    ) -> bool:
        return self._assets.load_watermarks(fullpage_paths, overlay_paths)

    def close_watermarks(self) -> None:
        self._assets.close_watermarks()

    # ------------------------------------------------------------------
    # In-memory watermark application (no disk I/O)
    # ------------------------------------------------------------------

    def watermark_image(
        self,
        image: Image.Image,
        settings: dict,
        *,
        is_first: bool = False,
        is_last: bool = False,
    ) -> Image.Image | None:
        """Apply watermarks to an in-memory PIL image. Returns modified image or None if no change."""
        fullpage_enabled = settings.get("watermark_fullpage_enabled", False)
        overlay_enabled = settings.get("watermark_overlay_enabled", False)
        should_apply_fullpage = fullpage_enabled and bool(self._assets.watermarks_fullpage)
        should_apply_overlay = overlay_enabled and bool(self._assets.watermarks_overlay)
        should_add_header = is_first and bool(settings.get("add_header", False)) and bool(
            settings.get("header_images")
        )
        should_add_footer = is_last and bool(settings.get("add_footer", False)) and bool(
            settings.get("footer_images")
        )

        if (
            not should_apply_fullpage
            and not should_apply_overlay
            and not should_add_header
            and not should_add_footer
        ):
            return None

        result = image.convert("RGBA")
        modified = False

        if should_apply_fullpage:
            fp_result = self._fullpage.add_watermark_fullpage(result, settings)
            if fp_result:
                _safe_close(result)
                result = fp_result
                modified = True

        if should_apply_overlay:
            ov_result = self._overlay.add_watermark_overlay(result, settings)
            if ov_result:
                _safe_close(result)
                result = ov_result
                modified = True

        if should_add_header or should_add_footer:
            composed = compose_with_header_footer(
                result,
                settings.get("header_images", []) if should_add_header else [],
                settings.get("footer_images", []) if should_add_footer else [],
            )
            if composed is not None:
                _safe_close(result)
                result = composed
                modified = True

        if modified:
            return result
        _safe_close(result)
        return None

    # ------------------------------------------------------------------
    # Chapter-level processing
    # ------------------------------------------------------------------

    def process_chapter_folder(
        self,
        chapter_path: str,
        settings: dict,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> bool:
        """Process a chapter folder applying watermarks to all images.

        Args:
            chapter_path: Path to the chapter folder
            settings: Watermark settings dictionary
            progress_callback: Optional callback(current, total, message)

        Returns:
            True if processing was successful
        """
        if not os.path.isdir(chapter_path):
            _wm_dbg(f"process_chapter_folder aborted: invalid chapter_path='{chapter_path}'")
            return False

        images = natsorted(
            [
                f
                for f in os.listdir(chapter_path)
                if f.lower().endswith((".png", ".avif", ".jpg", ".jpeg", ".webp"))
            ]
        )

        if not images:
            _wm_dbg(f"process_chapter_folder aborted: no images in '{chapter_path}'")
            return False

        chapter_skip = int(settings.get("watermark_chapter_skip", 0))
        if chapter_skip > 1:
            _wm_dbg(
                f"chapter skip: interval={chapter_skip}, "
                f"before_filter={len(images)}"
            )
            images = [img for i, img in enumerate(images) if i % chapter_skip == 0]
            _wm_dbg(f"chapter skip: after_filter={len(images)}")

        total_images = len(images)
        fullpage_enabled = settings.get("watermark_fullpage_enabled", False)
        overlay_enabled = settings.get("watermark_overlay_enabled", False)
        requested_workers_raw = settings.get(
            "watermark_max_workers", _WM_WORKERS_DEFAULT
        )
        try:
            requested_workers = int(requested_workers_raw)
        except (TypeError, ValueError):
            requested_workers = _WM_WORKERS_DEFAULT

        max_workers = max(1, min(requested_workers, _WM_WORKERS_LIMIT, total_images))
        self._last_run_info = {
            "requested_workers": requested_workers,
            "used_workers": max_workers,
            "parallel": max_workers > 1,
            "total_images": total_images,
        }
        _wm_dbg(
            f"process_chapter_folder start: chapter='{chapter_path}', total_images={total_images}, "
            f"fullpage_enabled={fullpage_enabled}, overlay_enabled={overlay_enabled}, "
            f"add_header={settings.get('add_header', False)}, add_footer={settings.get('add_footer', False)}, "
            f"max_workers={max_workers}"
        )

        def _process_one(
            i: int, image_name: str
        ) -> tuple[int, str, Optional[Exception]]:
            image_path = os.path.join(chapter_path, image_name)
            try:
                _wm_dbg(f"processing image {i + 1}/{total_images}: '{image_path}'")
                image_settings = dict(settings)
                image_settings["add_header"] = (
                    bool(settings.get("add_header", False)) and i == 0
                )
                image_settings["add_footer"] = bool(
                    settings.get("add_footer", False)
                ) and i == (total_images - 1)
                self._apply_watermarks_to_image(
                    image_path,
                    image_settings,
                    fullpage_enabled,
                    overlay_enabled,
                )
                return i, image_name, None
            except Exception as e:
                return i, image_name, e

        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_process_one, i, image_name): (i, image_name)
                for i, image_name in enumerate(images)
            }
            for future in concurrent.futures.as_completed(futures):
                _, image_name, error = future.result()
                completed += 1
                if progress_callback:
                    progress_callback(
                        completed, total_images, f"Processing {image_name}"
                    )
                if error is not None:
                    image_path = os.path.join(chapter_path, image_name)
                    _wm_dbg(f"processing failed for '{image_path}': {error}")
                    for pending in futures:
                        if pending is not future:
                            pending.cancel()
                    raise RuntimeError(
                        f"Error processing {image_path}: {error}"
                    ) from error

        gc.collect()
        _wm_dbg(f"process_chapter_folder done: chapter='{chapter_path}'")
        return True

    # ------------------------------------------------------------------
    # Per-image watermark application
    # ------------------------------------------------------------------

    def _apply_watermarks_to_image(
        self,
        image_path: str,
        settings: dict,
        fullpage_enabled: bool,
        overlay_enabled: bool,
    ) -> None:
        """Apply watermarks to a single image file."""
        _wm_dbg(
            f"_apply_watermarks_to_image start: image_path='{image_path}', "
            f"fullpage_enabled={fullpage_enabled}, overlay_enabled={overlay_enabled}, "
            f"loaded_fullpage={len(self._assets.watermarks_fullpage)}, "
            f"loaded_overlay={len(self._assets.watermarks_overlay)}"
        )

        should_apply_fullpage = fullpage_enabled and bool(self._assets.watermarks_fullpage)
        should_apply_overlay = overlay_enabled and bool(self._assets.watermarks_overlay)
        should_add_header = bool(settings.get("add_header", False)) and bool(
            settings.get("header_images")
        )
        should_add_footer = bool(settings.get("add_footer", False)) and bool(
            settings.get("footer_images")
        )
        if (
            not should_apply_fullpage
            and not should_apply_overlay
            and not should_add_header
            and not should_add_footer
        ):
            _wm_dbg("_apply_watermarks_to_image skipped: no loaded/active visual watermarks")
            return

        with Image.open(image_path) as img:
            src_icc = img.info.get("icc_profile")
            src_exif = img.info.get("exif")
            result = img.convert("RGBA")
            modified = False

            if should_apply_fullpage:
                fp_result = self._fullpage.add_watermark_fullpage(result, settings)
                if fp_result:
                    _safe_close(result)
                    result = fp_result
                    modified = True
                    _wm_dbg(
                        f"fullpage watermark applied: image_path='{image_path}', result_size={result.size}"
                    )
                else:
                    _wm_dbg(
                        f"fullpage watermark NOT applied: image_path='{image_path}'"
                    )
            else:
                _wm_dbg(
                    f"fullpage watermark skipped: enabled={fullpage_enabled}, "
                    f"loaded={len(self._assets.watermarks_fullpage)}"
                )

            if should_apply_overlay:
                ov_result = self._overlay.add_watermark_overlay(result, settings)
                if ov_result:
                    _safe_close(result)
                    result = ov_result
                    modified = True
                    _wm_dbg(
                        f"overlay watermark applied: image_path='{image_path}', result_size={result.size}"
                    )
                else:
                    _wm_dbg(
                        f"overlay watermark NOT applied: image_path='{image_path}'"
                    )
            else:
                _wm_dbg(
                    f"overlay watermark skipped: enabled={overlay_enabled}, "
                    f"loaded={len(self._assets.watermarks_overlay)}"
                )

            if should_add_header or should_add_footer:
                composed = compose_with_header_footer(
                    result,
                    settings.get("header_images", []) if should_add_header else [],
                    settings.get("footer_images", []) if should_add_footer else [],
                )
                if composed is not None:
                    _safe_close(result)
                    result = composed
                    modified = True
                    _wm_dbg(
                        f"header/footer added: image_path='{image_path}', "
                        f"header={should_add_header}, footer={should_add_footer}, result_size={result.size}"
                    )

            if modified:
                ext = os.path.splitext(image_path)[1].lower()
                if ext in (".png", ".webp", ".avif") and result.mode == "RGBA":
                    to_save = result
                    close_to_save = False
                elif result.mode == "RGBA":
                    to_save = Image.new("RGB", result.size, (255, 255, 255))
                    to_save.paste(result, mask=result.split()[-1])
                    close_to_save = True
                elif result.mode != "RGB":
                    to_save = result.convert("RGB")
                    close_to_save = True
                else:
                    to_save = result
                    close_to_save = False

                lossy_quality = int(settings.get("lossy_quality", 100))
                try:
                    save_result_image(
                        image_path,
                        to_save,
                        lossy_quality=lossy_quality,
                        src_icc=src_icc,
                        src_exif=src_exif,
                    )
                finally:
                    if close_to_save:
                        _safe_close(to_save)
                _wm_dbg(
                    f"saved modified image: path='{image_path}', ext='{ext}', "
                    f"size={result.size}, mode={to_save.mode}"
                )
            else:
                _wm_dbg(
                    f"image unchanged (no watermark applied): path='{image_path}'"
                )

            _safe_close(result)
            _wm_dbg(f"_apply_watermarks_to_image done: image_path='{image_path}'")
