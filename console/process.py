"""Console-based stitch process using the unified pipeline."""
import gc
import os
from dataclasses import dataclass
from time import time

from core.detectors import select_detector
from core.services import (
    DirectoryExplorer,
    ImageHandler,
    ImageManipulator,
    PerfBenchmark,
    is_benchmark_enabled,
    logFunc,
)
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
        benchmark = PerfBenchmark(
            mode="console",
            enabled=is_benchmark_enabled(),
            metadata={
                "input_folder": str(kwargs.get("input_folder") or ""),
                "output_type": settings.output_type,
                "detection_type": settings.detection_type,
                "custom_width": settings.custom_width,
            },
        )
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
            stage_seconds: dict[str, float] = {
                "load": 0.0,
                "resize": 0.0,
                "combine": 0.0,
                "detect": 0.0,
                "slice": 0.0,
                "save": 0.0,
            }
            retries = 0
            img_count = 0

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

                    stage_start = time()
                    imgs = img_handler.load(work_dir)
                    stage_seconds["load"] += time() - stage_start

                    stage_start = time()
                    imgs = img_manipulator.resize(imgs, width_enforce_mode, settings.custom_width)
                    stage_seconds["resize"] += time() - stage_start

                    print(f"[{idx}/{total}] Combining images into a single combined image")
                    stage_start = time()
                    combined_img = img_manipulator.combine(imgs)
                    stage_seconds["combine"] += time() - stage_start

                    print(f"[{idx}/{total}] Detecting & selecting valid slicing points")
                    stage_start = time()
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
                    stage_seconds["detect"] += time() - stage_start

                    print(f"[{idx}/{total}] Generating sliced output images in memory")
                    stage_start = time()
                    sliced = img_manipulator.slice(combined_img, slice_points)
                    stage_seconds["slice"] += time() - stage_start

                    print(f"[{idx}/{total}] Saving output images to storage")
                    img_count = len(sliced)
                    stage_start = time()
                    img_handler.save_all(
                        work_dir,
                        sliced,
                        img_format=settings.output_type,
                        quality=settings.lossy_quality,
                    )
                    stage_seconds["save"] += time() - stage_start
                    print(f"[{idx}/{total}] {img_count} images saved successfully")
                    benchmark.add_directory(
                        input_path=work_dir.input_path,
                        output_path=work_dir.output_path,
                        image_count=img_count,
                        retries=retries,
                        stage_seconds=stage_seconds,
                        success=True,
                    )
                    break

                except Exception as exc:
                    if attempt >= _MAX_SENSITIVITY_RETRIES or not is_dimension_error(exc):
                        benchmark.add_directory(
                            input_path=work_dir.input_path,
                            output_path=work_dir.output_path,
                            image_count=img_count,
                            retries=retries,
                            stage_seconds=stage_seconds,
                            success=False,
                            error=str(exc),
                        )
                        raise

                    new_sensitivity = max(0, int(sensitivity * _SENSITIVITY_RETRY_FACTOR))
                    retries += 1
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
        benchmark_file = benchmark.write_json(file_prefix="benchmark", total_elapsed_s=elapsed)
        if benchmark_file and os.path.isfile(benchmark_file):
            print(f"Benchmark saved to: {benchmark_file}")
        print(f"--- Process completed in {elapsed:.3f} seconds ---")
