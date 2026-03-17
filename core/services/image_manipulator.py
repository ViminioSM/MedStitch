"""Image manipulation with controlled resource usage."""
from multiprocessing import cpu_count

from PIL import Image as pil

from ..utils.constants import WIDTH_ENFORCEMENT
from .global_logger import logFunc

_RESAMPLE_LANCZOS = getattr(getattr(pil, "Resampling", pil), "LANCZOS")


# Limit workers to prevent system overload
_MAX_WORKERS_LIMIT = 3


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
        
        Uses sequential processing to avoid:
        - Memory explosion from serializing images to bytes
        - Process spawning overhead
        - System instability
        
        For most use cases, sequential resize is fast enough since
        PIL resize is already optimized and I/O bound.
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
        
        # Sequential resize - safer and sufficient for most cases
        resized_imgs: list[pil.Image] = []
        for img in img_objs:
            if img.size[0] != new_img_width:
                img_ratio = img.size[1] / img.size[0]
                new_img_height = int(img_ratio * new_img_width)
                if new_img_height > 0:
                    resized = img.resize((new_img_width, new_img_height), _RESAMPLE_LANCZOS)
                    img.close()
                    resized_imgs.append(resized)
                else:
                    resized_imgs.append(img)
            else:
                resized_imgs.append(img)
        
        return resized_imgs

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
