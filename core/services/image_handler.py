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
_MAX_WORKERS_LIMIT = 4
_DEFAULT_TIMEOUT_SECONDS = 5  # 5 seconds per operation


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
        # Limit workers to prevent system overload
        cpu = cpu_count() or 2
        default_workers = min(cpu, _MAX_WORKERS_LIMIT)
        self.max_workers = min(max_workers or default_workers, _MAX_WORKERS_LIMIT)

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
                img_obj.save(full_path, quality=quality, subsampling=0)
            elif effective_format.lower() == ".webp":
                img_obj.save(full_path, quality=quality, method=4)
            elif effective_format.lower() == ".png":
                img_obj.save(full_path, compress_level=0)
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
                    img.save(path, quality=quality, subsampling=0)
                elif ext == ".webp":
                    img.save(path, quality=quality, method=4)
                elif ext == ".png":
                    img.save(path, compress_level=0)
                else:
                    img.save(path)
            img.close()

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [
                executor.submit(_save_one, img, path)
                for img, path in zip(img_objs, full_paths)
            ]
            for fut in as_completed(futures):
                fut.result()

        workdirectory.output_files.extend(file_names)
        return workdirectory
