"""Image loading and saving with controlled parallelism."""
import io
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import cpu_count

from PIL import Image as pil
from PIL import UnidentifiedImageError
from psd_tools import PSDImage

from ..models import WorkDirectory
from .global_logger import logFunc
from ..utils.constants import PHOTOSHOP_FILE_TYPES


_MAX_PIL_IMAGE_DIMENSION = 30000
# Limit workers to prevent system overload
_MAX_LOAD_WORKERS_LIMIT = 16
_MAX_SAVE_WORKERS_LIMIT = 20
_DEFAULT_TIMEOUT_SECONDS = 5  # 5 seconds per operation


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


def _load_image_worker(args: tuple) -> tuple[bool, str, bytes | None, str | None]:
    """Worker function to load a single image and return (ok, path, bytes, err).

    Must be a module-level function so it is picklable by ProcessPoolExecutor.
    """
    img_path, psd_first_layer_only = args
    ext = os.path.splitext(img_path)[1].lower()

    try:
        if ext not in PHOTOSHOP_FILE_TYPES:
            image = pil.open(img_path)
            image.load()
        else:
            psd = PSDImage.open(img_path)
            if psd_first_layer_only and len(psd) > 0:
                image = psd[0].topil()
            else:
                image = psd.topil()

        if image is None:
            raise ValueError(f"Unable to decode image: {img_path}")

        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        try:
            image.close()
        except Exception:
            pass
        return True, img_path, buf.getvalue(), None
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return False, img_path, None, str(exc)
    except Exception as exc:
        return False, img_path, None, repr(exc)


class ImageHandler:
    """Handles image loading and saving with controlled parallelism."""

    def __init__(self, max_workers: int | None = None) -> None:
        # Load/decode workers are moderate; save workers can be higher for I/O throughput.
        cpu = cpu_count() or 2
        default_load_workers = min(cpu, _MAX_LOAD_WORKERS_LIMIT)
        self.max_workers = min(max_workers or default_load_workers, _MAX_LOAD_WORKERS_LIMIT)

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
            auto_save_workers = max(self.max_workers * 2, min(cpu * 2, _MAX_SAVE_WORKERS_LIMIT))
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
            os.path.join(workdirectory.input_path, f)
            for f in workdirectory.input_files
        ]

        images: list[pil.Image | None] = [None] * len(img_paths)
        errors: list[str] = []

        def _load_single(idx: int, path: str) -> None:
            """Load a single image in thread."""
            ext = os.path.splitext(path)[1].lower()
            try:
                if ext not in PHOTOSHOP_FILE_TYPES:
                    image = pil.open(path)
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
                executor.submit(_load_single, i, p)
                for i, p in enumerate(img_paths)
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
                    except Exception:
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
        if img_format.lower() in (".jpg", ".jpeg") and _should_fallback_from_jpeg(img_obj):
            effective_format = ".png"

        file_name = f"{img_iteration:02}{effective_format}"
        full_path = os.path.join(workdirectory.output_path, file_name)

        if effective_format in PHOTOSHOP_FILE_TYPES:
            PSDImage.frompil(img_obj).save(full_path)
        else:
            if effective_format.lower() in (".jpg", ".jpeg"):
                img_obj.save(full_path, quality=quality, subsampling=self.jpeg_subsampling, optimize=False)
            elif effective_format.lower() == ".webp":
                img_obj.save(full_path, quality=quality, method=self.webp_method)
            elif effective_format.lower() == ".png":
                img_obj.save(full_path, compress_level=self.png_compress_level)
            else:
                img_obj.save(full_path)
            img_obj.close()

        workdirectory.output_files.append(file_name)
        return file_name

    def save_all(
        self,
        workdirectory: WorkDirectory,
        img_objs: list[pil.Image],
        img_format: str = ".png",
        quality: int = 100,
    ) -> WorkDirectory:
        """Save all images using threads (I/O-bound, no serialization overhead)."""
        os.makedirs(workdirectory.output_path, exist_ok=True)

        def _effective_format_for(img: pil.Image) -> str:
            if img_format.lower() in (".jpg", ".jpeg") and _should_fallback_from_jpeg(img):
                return ".png"
            return img_format

        file_names: list[str] = [
            f"{i + 1:02}{_effective_format_for(img)}" for i, img in enumerate(img_objs)
        ]
        full_paths = [os.path.join(workdirectory.output_path, fn) for fn in file_names]

        def _save_one(img: pil.Image, path: str) -> None:
            ext = os.path.splitext(path)[1].lower()
            if ext in PHOTOSHOP_FILE_TYPES:
                PSDImage.frompil(img).save(path)
            else:
                if ext in (".jpg", ".jpeg"):
                    img.save(path, quality=quality, subsampling=self.jpeg_subsampling, optimize=False)
                elif ext == ".webp":
                    img.save(path, quality=quality, method=self.webp_method)
                elif ext == ".png":
                    img.save(path, compress_level=self.png_compress_level)
                else:
                    img.save(path)
            img.close()

        save_pool_workers = max(1, min(self.save_workers, len(img_objs)))

        with ThreadPoolExecutor(max_workers=save_pool_workers) as executor:
            futures = [
                executor.submit(_save_one, img, path)
                for img, path in zip(img_objs, full_paths)
            ]
            for fut in as_completed(futures):
                fut.result()

        workdirectory.output_files.extend(file_names)
        return workdirectory
