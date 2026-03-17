"""Console-based stitch process using the unified pipeline."""
import gc
from dataclasses import dataclass
from time import time

from core.detectors import select_detector
from core.services import DirectoryExplorer, ImageHandler, ImageManipulator, logFunc
from core.utils.constants import WIDTH_ENFORCEMENT
from core.utils.image_utils import (
    _MAX_PIL_IMAGE_DIMENSION,
    _MAX_SENSITIVITY_RETRIES,
    _SENSITIVITY_RETRY_FACTOR,
    close_images_safely,
    ensure_max_slice_segment,
    is_dimension_error,
)


@dataclass
class ConsoleSettings:
    """Settings container for console process."""
    split_height: int
    output_type: str
    lossy_quality: int
    custom_width: int
    detection_type: str
    sensitivity: int
    ignorable_pixels: int
    scan_step: int

    @classmethod
    def from_kwargs(cls, kwargs: dict) -> "ConsoleSettings":
        return cls(
            split_height=kwargs.get("split_height", 5000),
            output_type=kwargs.get("output_type", ".png"),
            lossy_quality=kwargs.get("lossy_quality", 100),
            custom_width=kwargs.get("custom_width", -1),
            detection_type=kwargs.get("detection_type", "pixel"),
            sensitivity=kwargs.get("detection_sensitivity", 90),
            ignorable_pixels=kwargs.get("ignorable_pixels", 5),
            scan_step=kwargs.get("scan_line_step", 5),
        )


class ConsoleStitchProcess:
    @logFunc(inclass=True)
    def run(self, kwargs: dict):
        settings = ConsoleSettings.from_kwargs(kwargs)
        explorer = DirectoryExplorer()

        width_enforce_mode = (
            WIDTH_ENFORCEMENT.MANUAL
            if settings.custom_width > 0
            else WIDTH_ENFORCEMENT.NONE
        )

        start_time = time()
        print("--- Process Starting Up ---")
        print("Exploring input directory for working directories")
        input_folder = str(kwargs.get("input_folder") or "").strip()
        if not input_folder:
            raise ValueError("Missing input folder.")
        input_dirs = explorer.run(input=input_folder)
        total = len(input_dirs)
        print(f"[{total}] Working directories were found")

        for idx, work_dir in enumerate(input_dirs, 1):
            print(f"-> Starting stitching process for working directory #{idx} <-")

            sensitivity = settings.sensitivity
            scan_step = settings.scan_step
            ignorable_pixels = settings.ignorable_pixels

            for attempt in range(_MAX_SENSITIVITY_RETRIES + 1):
                imgs = None
                combined_img = None
                sliced = None
                try:
                    print(f"[{idx}/{total}] Preparing & loading images into memory")
                    img_handler = ImageHandler()
                    img_manipulator = ImageManipulator()
                    detector = select_detector(detection_type=settings.detection_type)

                    imgs = img_handler.load(work_dir)
                    imgs = img_manipulator.resize(imgs, width_enforce_mode, settings.custom_width)

                    print(f"[{idx}/{total}] Combining images into a single combined image")
                    combined_img = img_manipulator.combine(imgs)

                    print(f"[{idx}/{total}] Detecting & selecting valid slicing points")
                    slice_points = detector.run(
                        combined_img,
                        settings.split_height,
                        sensitivity=sensitivity,
                        ignorable_pixels=ignorable_pixels,
                        scan_step=scan_step,
                    )
                    if settings.output_type.lower() in (".jpg", ".jpeg"):
                        slice_points = ensure_max_slice_segment(
                            slice_points,
                            combined_height=combined_img.size[1],
                            max_segment=_MAX_PIL_IMAGE_DIMENSION,
                        )

                    print(f"[{idx}/{total}] Generating sliced output images in memory")
                    sliced = img_manipulator.slice(combined_img, slice_points)

                    print(f"[{idx}/{total}] Saving output images to storage")
                    img_count = len(sliced)
                    img_handler.save_all(
                        work_dir,
                        sliced,
                        img_format=settings.output_type,
                        quality=settings.lossy_quality,
                    )
                    print(f"[{idx}/{total}] {img_count} images saved successfully")
                    break

                except Exception as exc:
                    if attempt >= _MAX_SENSITIVITY_RETRIES or not is_dimension_error(exc):
                        raise

                    new_sensitivity = max(0, int(sensitivity * _SENSITIVITY_RETRY_FACTOR))
                    print(
                        f"Retrying folder '{work_dir.input_path}' due to large image output. "
                        f"Adjusting sensitivity {sensitivity} → {new_sensitivity}, scan_step → 5, "
                        f"ignorable_pixels → 5 (attempt {attempt + 1}/{_MAX_SENSITIVITY_RETRIES})."
                    )
                    sensitivity = new_sensitivity
                    scan_step = 5
                    ignorable_pixels = 5

                finally:
                    close_images_safely(sliced, combined_img, imgs)

            gc.collect()

        elapsed = time() - start_time
        print(f"--- Process completed in {elapsed:.3f} seconds ---")
