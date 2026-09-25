"""Image loading and saving with controlled parallelism."""

import io
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import cpu_count

from PIL import Image as pil
from PIL import UnidentifiedImageError
from psd_tools import PSDImage

try:
    import pillow_avif  # type: ignore # Registers AVIF support in Pillow when installed.
except Exception:
    pillow_avif = None

try:
    import cairosvg  # type: ignore # Rasterizes SVG files for input-only support.
except Exception:
    cairosvg = None

from ..models import WorkDirectory
from ..utils.constants import PHOTOSHOP_FILE_TYPES
from .global_logger import logFunc

_MAX_PIL_IMAGE_DIMENSION = 30000
# Worker limits scaled by CPU cores (no artificial cap)
_MAX_LOAD_WORKERS_LIMIT = max(16, (os.cpu_count() or 8) * 2)
_MAX_SAVE_WORKERS_LIMIT = max(20, (os.cpu_count() or 8) * 2)
# Per-future wait after as_completed; large PSD/scans can exceed a few seconds.
_DEFAULT_TIMEOUT_SECONDS = 300


def _read_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    value = (os.getenv(name) or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(minimum, min(parsed, maximum))


def _read_bool_env(name: str, default: bool = False) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _should_fallback_from_jpeg(img: pil.Image) -> bool:
    return max(img.size) > _MAX_PIL_IMAGE_DIMENSION


def _open_image_with_svg_support(img_path: str) -> pil.Image:
    ext = os.path.splitext(img_path)[1].lower()
    if ext != ".svg":
        return pil.open(img_path)

    if cairosvg is None:
        raise RuntimeError(
            "SVG input requires the 'cairosvg' package. Install dependencies and try again."
        )

    with open(img_path, "rb") as svg_file:
        svg_bytes = svg_file.read()
    png_bytes = cairosvg.svg2png(bytestring=svg_bytes)
    return pil.open(io.BytesIO(png_bytes))


def _prepare_image_for_save(img: pil.Image, ext: str) -> pil.Image:
    """Ensure image mode is compatible with the target format (e.g. JPEG needs RGB)."""
    lower = ext.lower()
    if lower in (".jpg", ".jpeg", ".jfif"):
        if img.mode in ("RGBA", "LA"):
            background = pil.new("RGB", img.size, (255, 255, 255))
            alpha = img.split()[-1]
            background.paste(img.convert("RGBA"), mask=alpha)
            return background
        if img.mode != "RGB":
            return img.convert("RGB")
    return img


class ImageHandler:
    """Handles image loading and saving with controlled parallelism."""

    def __init__(self, max_workers: int | None = None) -> None:
        # Load/decode workers are moderate; save workers can be higher for I/O throughput.
        cpu = cpu_count() or 2
        default_load_workers = min(cpu, _MAX_LOAD_WORKERS_LIMIT)
        self.max_workers = min(
            max_workers or default_load_workers, _MAX_LOAD_WORKERS_LIMIT
        )

        load_workers_env = (os.getenv("SMARTSTITCH_LOAD_WORKERS") or "").strip()
        if load_workers_env:
            try:
                configured_load = int(load_workers_env)
            except ValueError:
                configured_load = self.max_workers
            self.max_workers = max(1, min(configured_load, _MAX_LOAD_WORKERS_LIMIT))

        save_workers_env = (os.getenv("SMARTSTITCH_SAVE_WORKERS") or "").strip()
        if save_workers_env:
            try:
                configured = int(save_workers_env)
            except ValueError:
                configured = self.max_workers
            self.save_workers = max(1, min(configured, _MAX_SAVE_WORKERS_LIMIT))
        else:
            auto_save_workers = max(
                self.max_workers * 2, min(cpu * 2, _MAX_SAVE_WORKERS_LIMIT)
            )
            self.save_workers = max(1, min(auto_save_workers, _MAX_SAVE_WORKERS_LIMIT))

        # Encoding knobs: lowering encode complexity often improves save time more than adding threads.
        fast_save = _read_bool_env("SMARTSTITCH_FAST_SAVE", default=False)
        default_jpeg_subsampling = 2 if fast_save else 0
        default_webp_method = 0 if fast_save else 4

        self.jpeg_subsampling = _read_int_env(
            "SMARTSTITCH_JPEG_SUBSAMPLING",
            default=default_jpeg_subsampling,
            minimum=0,
            maximum=2,
        )
        self.webp_method = _read_int_env(
            "SMARTSTITCH_WEBP_METHOD",
            default=default_webp_method,
            minimum=0,
            maximum=6,
        )
        self.png_compress_level = _read_int_env(
            "SMARTSTITCH_PNG_COMPRESS_LEVEL",
            default=0,
            minimum=0,
            maximum=9,
        )

    @logFunc(inclass=True)
    def load(
        self,
        workdirectory: WorkDirectory,
        psd_first_layer_only: bool = False,
    ) -> list[pil.Image]:
        """Load all images in *workdirectory* using threads (safer than processes).

        Uses ThreadPoolExecutor instead of ProcessPoolExecutor to avoid:
        - Excessive memory usage from serialization
        - Process spawning overhead
        - System instability from too many processes

        Raises RuntimeError if any file is invalid/corrupted.
        """
        img_paths = [
            os.path.join(workdirectory.input_path, f) for f in workdirectory.input_files
        ]

        images: list[pil.Image | None] = [None] * len(img_paths)
        errors: list[str] = []

        def _load_single(idx: int, path: str) -> None:
            """Load a single image in thread."""
            ext = os.path.splitext(path)[1].lower()
            try:
                if ext not in PHOTOSHOP_FILE_TYPES:
                    image = _open_image_with_svg_support(path)
                    image.load()  # Force load into memory
                else:
                    psd = PSDImage.open(path)
                    if psd_first_layer_only and len(psd) > 0:
                        image = psd[0].topil()
                    else:
                        image = psd.topil()

                if image is None:
                    raise ValueError(f"Unable to decode image: {path}")

                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGB")

                images[idx] = image
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                errors.append(f"{path}: {exc}")
            except Exception as exc:
                errors.append(f"{path}: {repr(exc)}")

        # Use threads instead of processes - safer and sufficient for I/O
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [
                executor.submit(_load_single, i, p) for i, p in enumerate(img_paths)
            ]
            for fut in as_completed(futures):
                try:
                    fut.result(timeout=_DEFAULT_TIMEOUT_SECONDS)
                except Exception as exc:
                    errors.append(f"Load timeout or error: {exc}")

        if errors:
            # Close any successfully loaded images before raising
            for img in images:
                if img is not None:
                    try:
                        img.close()
                    except Exception:  # nosec B110
                        pass
            raise RuntimeError(
                "Invalid/corrupted image detected. Folder processing aborted.\n"
                + "\n".join(errors[:10])
            )

        valid = [img for img in images if img is not None]
        if not valid:
            raise RuntimeError("No valid images could be decoded in this folder.")

        return valid

    @logFunc(inclass=True)
    def save(
        self,
        workdirectory: WorkDirectory,
        img_obj: pil.Image,
        img_iteration: int = 1,
        img_format: str = ".png",
        quality: int = 100,
    ) -> str:
        os.makedirs(workdirectory.output_path, exist_ok=True)
        effective_format = img_format
        if img_format.lower() in (".jpg", ".jpeg") and _should_fallback_from_jpeg(
            img_obj
        ):
            effective_format = ".png"

        file_name = f"{img_iteration:02}{effective_format}"
        full_path = os.path.join(workdirectory.output_path, file_name)

        quality = max(1, min(100, int(quality)))
        if effective_format in PHOTOSHOP_FILE_TYPES:
            PSDImage.frompil(img_obj).save(full_path)
        else:
            to_save = _prepare_image_for_save(img_obj, effective_format)
            try:
                if effective_format.lower() in (".jpg", ".jpeg"):
                    to_save.save(
                        full_path,
                        quality=quality,
                        subsampling=self.jpeg_subsampling,
                        optimize=False,
                    )
                elif effective_format.lower() == ".avif":
                    to_save.save(full_path, quality=quality, lossless=False)
                elif effective_format.lower() == ".webp":
                    to_save.save(full_path, quality=quality, method=self.webp_method)
                elif effective_format.lower() == ".png":
                    to_save.save(full_path, compress_level=self.png_compress_level)
                else:
                    to_save.save(full_path)
            finally:
                if to_save is not img_obj:
                    try:
                        to_save.close()
                    except Exception:  # nosec B110
                        pass
            img_obj.close()

        workdirectory.output_files.append(file_name)
        return file_name

    def save_all(
        self,
        workdirectory: WorkDirectory,
        img_objs: list[pil.Image],
        img_format: str = ".png",
        quality: int = 100,
        extra_formats: list[str] | None = None,
    ) -> WorkDirectory:
        """Save all images using threads (I/O-bound, no serialization overhead).

        When *extra_formats* is given (e.g. dual WEBP+PNG), every image is saved
        once in *img_format* plus once per extra format into the SAME output
        folder (``01.webp`` + ``01.png``). Stitching/detection runs only once —
        only the final encode is duplicated.
        """
        os.makedirs(workdirectory.output_path, exist_ok=True)

        norm_primary = (img_format or ".png").lower()
        norm_extra = [
            str(e or "").lower()
            for e in (extra_formats or [])
            if str(e or "").strip()
        ]
        # De-dup extras: skip anything equal to the primary format.
        norm_extra = [e for e in norm_extra if e and e != norm_primary]

        def _effective_format_for(img: pil.Image) -> str:
            if img_format.lower() in (".jpg", ".jpeg") and _should_fallback_from_jpeg(
                img
            ):
                return ".png"
            return img_format

        file_names: list[str] = [
            f"{i + 1:02}{_effective_format_for(img)}" for i, img in enumerate(img_objs)
        ]
        extra_file_names: list[list[str]] = [
            [f"{i + 1:02}{ext}" for ext in norm_extra]
            for i, _img in enumerate(img_objs)
        ]
        full_paths = [os.path.join(workdirectory.output_path, fn) for fn in file_names]
        extra_full_paths: list[list[str]] = [
            [os.path.join(workdirectory.output_path, fn) for fn in names]
            for names in extra_file_names
        ]

        quality = max(1, min(100, int(quality)))

        def _save_path(img: pil.Image, path: str) -> None:
            ext = os.path.splitext(path)[1].lower()
            if ext in PHOTOSHOP_FILE_TYPES:
                PSDImage.frompil(img).save(path)
                return
            to_save = _prepare_image_for_save(img, ext)
            try:
                if ext in (".jpg", ".jpeg"):
                    to_save.save(
                        path,
                        quality=quality,
                        subsampling=self.jpeg_subsampling,
                        optimize=False,
                    )
                elif ext == ".avif":
                    to_save.save(path, quality=quality, lossless=False)
                elif ext == ".webp":
                    to_save.save(path, quality=quality, method=self.webp_method)
                elif ext == ".png":
                    to_save.save(path, compress_level=self.png_compress_level)
                else:
                    to_save.save(path)
            finally:
                if to_save is not img:
                    try:
                        to_save.close()
                    except Exception:  # nosec B110
                        pass

        def _save_one(img: pil.Image, path: str, extra_paths: list[str]) -> None:
            try:
                _save_path(img, path)
                for extra_path in extra_paths:
                    _save_path(img, extra_path)
            finally:
                img.close()

        save_pool_workers = max(1, min(self.save_workers, len(img_objs)))
        # Huge slices each hold Wxh bytes; too many concurrent encodes
        # multiplies peak RAM and thrashes. Identical bytes, only scheduling.
        try:
            max_h = max((img.size[1] for img in img_objs), default=0)
            if max_h > 8000:
                cap = max(1, (os.cpu_count() or 4) // 2)
                save_pool_workers = max(1, min(save_pool_workers, cap))
        except Exception:
            pass

        with ThreadPoolExecutor(max_workers=save_pool_workers) as executor:
            futures = [
                executor.submit(_save_one, img, path, extras)
                for img, path, extras in zip(
                    img_objs, full_paths, extra_full_paths, strict=True
                )
            ]
            for fut in as_completed(futures):
                fut.result()

        flat_files = list(file_names)
        for names in extra_file_names:
            flat_files.extend(names)
        workdirectory.output_files.extend(flat_files)
        return workdirectory
