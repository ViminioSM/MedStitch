"""Image manipulation with controlled resource usage."""
import os
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import cpu_count

from PIL import Image as pil

from ..utils.constants import WIDTH_ENFORCEMENT
from .global_logger import logFunc

_RESAMPLE_LANCZOS = getattr(getattr(pil, "Resampling", pil), "LANCZOS")


# Limit workers to prevent system overload
_MAX_WORKERS_LIMIT = 3


def _resize_workers_default() -> int:
    override = (os.getenv("SMARTSTITCH_RESIZE_WORKERS") or "").strip()
    if override:
        try:
            return max(1, min(int(override), max(8, (os.cpu_count() or 4))))
        except ValueError:
            pass
    # Threads share memory (no serialization); PIL resize releases the GIL in C,
    # so parallelism is a pure win with identical LANCZOS pixels.
    return max(1, min((os.cpu_count() or 4), 8))


class ImageManipulator:
    """Handles image resizing, combining, and slicing operations.
    
    Uses sequential processing for resize to avoid memory issues
    from serializing large images across processes.
    """

    def __init__(self, max_workers: int | None = None) -> None:
        """Initialize ImageManipulator with optional max_workers.
        
        Workers are limited to prevent system overload.
        """
        cpu = cpu_count() or 2
        default_workers = min(cpu, _MAX_WORKERS_LIMIT)
        self.max_workers = min(max_workers or default_workers, _MAX_WORKERS_LIMIT)

    @logFunc(inclass=True)
    def resize(
        self,
        img_objs: list[pil.Image],
        enforce_setting: int | WIDTH_ENFORCEMENT,
        custom_width: int = 720,
    ) -> list[pil.Image]:
        """Resizes all given images according to the set enforcement setting.

        Thread-parallel LANCZOS with identical pixels (order preserved).
        Threads share memory and PIL releases the GIL in C, so unlike
        processes there is no serialization overhead.
        """
        if int(enforce_setting) == int(WIDTH_ENFORCEMENT.NONE):
            return img_objs
        
        # Determine target width
        new_img_width = 0
        if int(enforce_setting) == int(WIDTH_ENFORCEMENT.AUTOMATIC):
            widths = [img.size[0] for img in img_objs]
            new_img_width = min(widths)
        elif int(enforce_setting) == int(WIDTH_ENFORCEMENT.MANUAL):
            new_img_width = custom_width
        
        if new_img_width <= 0:
            return img_objs

        def _resize_one(img: pil.Image) -> pil.Image:
            if img.size[0] == new_img_width:
                return img
            img_ratio = img.size[1] / img.size[0]
            new_img_height = int(img_ratio * new_img_width)
            if new_img_height <= 0:
                return img
            resized = img.resize((new_img_width, new_img_height), _RESAMPLE_LANCZOS)
            try:
                img.close()
            except Exception:
                pass
            return resized

        needs = sum(1 for img in img_objs if img.size[0] != new_img_width)
        if needs <= 1:
            return [_resize_one(img) for img in img_objs]
        workers = max(1, min(_resize_workers_default(), needs))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(_resize_one, img_objs))

    @logFunc(inclass=True)
    def combine(self, img_objs: list[pil.Image]) -> pil.Image:
        """Combines given image objs to a single vertically stacked single image obj."""
        widths, heights = zip(*(img.size for img in img_objs))
        combined_img_width = max(widths)
        combined_img_height = sum(heights)
        combined_img = pil.new('RGB', (combined_img_width, combined_img_height))
        combine_offset = 0
        for img in img_objs:
            combined_img.paste(img, (0, combine_offset))
            combine_offset += img.size[1]
            img.close()
        return combined_img

    @logFunc(inclass=True)
    def slice(
        self, combined_img: pil.Image, slice_locations: list[int]
    ) -> list[pil.Image]:
        """Combines given combined img to into multiple img slices given the slice locations."""
        max_width = combined_img.size[0]
        img_objs = []
        for index in range(1, len(slice_locations)):
            upper_limit = slice_locations[index - 1]
            lower_limit = slice_locations[index]
            slice_boundaries = (0, upper_limit, max_width, lower_limit)
            img_slice = combined_img.crop(slice_boundaries)
            img_objs.append(img_slice)
        combined_img.close()
        return img_objs
