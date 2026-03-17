"""GUI stitch process with controlled parallelism."""
import gc
import concurrent.futures
import os
import threading
from dataclasses import dataclass
from time import time
from typing import Callable

from core.detectors import select_detector
from core.services import (
    DirectoryExplorer,
    ImageHandler,
    ImageManipulator,
    PerfBenchmark,
    PostProcessRunner,
    SettingsHandler,
    WatermarkService,
    is_benchmark_enabled,
    logFunc,
)
from core.utils.image_utils import (
    _MAX_PIL_IMAGE_DIMENSION,
    _MAX_SENSITIVITY_RETRIES,
    _SENSITIVITY_RETRY_FACTOR,
    close_images_safely,
    ensure_max_slice_segment,
    is_dimension_error,
)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
_COMICZIP_SCRIPT = os.path.join(_PROJECT_ROOT, "scripts", "comiczip.py")

# Parallel processing limits to prevent system overload
_MAX_PARALLEL_DIRECTORIES = 5  # Max directories to process simultaneously
_WM_DEBUG_ENABLED = os.getenv(
    "MEDSTITCH_WM_DEBUG",
    os.getenv("SMARTSTITCH_WM_DEBUG", "0"),
).strip().lower() in {"1", "true", "yes", "on"}

StatusFunc = Callable[[int | float, str], None]
ConsoleFunc = Callable[[str], None]


def _wm_run_log(console_func: ConsoleFunc, message: str) -> None:
    """Verbose watermark runtime diagnostics (disabled by default)."""
    if not _WM_DEBUG_ENABLED:
        return
    line = f"[WM-RUN] {message}"
    try:
        console_func(line + "\n")
    except Exception:
        pass
    print(line)


@dataclass(frozen=True)
class _SettingsSnapshot:
    """Immutable snapshot of all settings needed during a processing run.

    Loaded once to avoid repeated disk/JSON reads inside tight loops.
    """
    split_height: int
    output_type: str
    lossy_quality: int
    enforce_type: int
    enforce_width: int
    detector_type: int
    sensitivity: int
    ignorable_pixels: int
    scan_step: int
    run_postprocess: bool
    run_comiczip: bool
    parallel_processing: bool
    postprocess_app: str
    postprocess_args: str
    # Watermark
    watermark_fullpage_enabled: bool
    watermark_fullpage_paths: str
    watermark_fullpage_position: int
    watermark_fullpage_frequency: int
    watermark_fullpage_threshold: int
    watermark_fullpage_alternate_interval: int
    watermark_overlay_enabled: bool
    watermark_overlay_paths: str
    watermark_overlay_position: int
    watermark_overlay_opacity: int
    watermark_overlay_scale_pct: int
    watermark_overlay_max_per_page: int
    watermark_header_enabled: bool
    watermark_header_paths: str
    watermark_footer_enabled: bool
    watermark_footer_paths: str

    watermark_fullpage_max_per_page: int
    watermark_fullpage_block_strategy: int

    @property
    def has_watermark(self) -> bool:
        return (
            (self.watermark_fullpage_enabled and bool(self.watermark_fullpage_paths.strip()))
            or (self.watermark_overlay_enabled and bool(self.watermark_overlay_paths.strip()))
            or (self.watermark_header_enabled and bool(self.watermark_header_paths.strip()))
            or (self.watermark_footer_enabled and bool(self.watermark_footer_paths.strip()))
        )

    @classmethod
    def from_settings(cls, s: SettingsHandler) -> "_SettingsSnapshot":
        return cls(
            split_height=s.load("split_height"),
            output_type=s.load("output_type"),
            lossy_quality=s.load("lossy_quality"),
            enforce_type=s.load("enforce_type"),
            enforce_width=s.load("enforce_width"),
            detector_type=s.load("detector_type"),
            sensitivity=s.load("sensitivity"),
            ignorable_pixels=s.load("ignorable_pixels"),
            scan_step=s.load("scan_step"),
            run_postprocess=s.load("run_postprocess"),
            run_comiczip=s.load("run_comiczip"),
            parallel_processing=s.load("parallel_processing"),
            postprocess_app=s.load("postprocess_app"),
            postprocess_args=s.load("postprocess_args"),
            watermark_fullpage_enabled=s.load("watermark_fullpage_enabled"),
            watermark_fullpage_paths=s.load("watermark_fullpage_paths"),
            watermark_fullpage_position=s.load("watermark_fullpage_position"),
            watermark_fullpage_frequency=s.load("watermark_fullpage_frequency"),
            watermark_fullpage_threshold=s.load("watermark_fullpage_threshold"),
            watermark_fullpage_alternate_interval=s.load("watermark_fullpage_alternate_interval"),
            watermark_overlay_enabled=s.load("watermark_overlay_enabled"),
            watermark_overlay_paths=s.load("watermark_overlay_paths"),
            watermark_overlay_position=s.load("watermark_overlay_position"),
            watermark_overlay_opacity=s.load("watermark_overlay_opacity"),
            watermark_overlay_scale_pct=s.load("watermark_overlay_scale_pct"),
            watermark_overlay_max_per_page=s.load("watermark_overlay_max_per_page"),
            watermark_header_enabled=s.load("watermark_header_enabled"),
            watermark_header_paths=s.load("watermark_header_paths"),
            watermark_footer_enabled=s.load("watermark_footer_enabled"),
            watermark_footer_paths=s.load("watermark_footer_paths"),
            watermark_fullpage_max_per_page=s.load("watermark_fullpage_max_per_page"),
            watermark_fullpage_block_strategy=s.load("watermark_fullpage_block_strategy"),
        )


def _parse_paths(raw: str) -> list[str]:
    """Split a semicolon-separated path string into a list of existing paths."""
    return [p.strip() for p in raw.split(";") if p.strip() and os.path.isfile(p.strip())]


def _run_watermark(output_dir: str, snap: _SettingsSnapshot, console_func: ConsoleFunc = print) -> None:
    """Apply watermarks to all images in *output_dir* based on snapshot settings."""
    _wm_run_log(
        console_func,
        "Snapshot flags: "
        f"fullpage_enabled={snap.watermark_fullpage_enabled}, "
        f"overlay_enabled={snap.watermark_overlay_enabled}, "
        f"header_enabled={snap.watermark_header_enabled}, "
        f"footer_enabled={snap.watermark_footer_enabled}"
    )
    _wm_run_log(
        console_func,
        "Raw paths: "
        f"fullpage='{snap.watermark_fullpage_paths}', "
        f"overlay='{snap.watermark_overlay_paths}', "
        f"header='{snap.watermark_header_paths}', "
        f"footer='{snap.watermark_footer_paths}'"
    )

    if not snap.has_watermark:
        _wm_run_log(console_func, "Skipping watermark step: has_watermark=False")
        return

    wm_service = WatermarkService()
    v1_paths = _parse_paths(snap.watermark_fullpage_paths) if snap.watermark_fullpage_enabled else []
    v2_paths = _parse_paths(snap.watermark_overlay_paths) if snap.watermark_overlay_enabled else []
    header_paths = _parse_paths(snap.watermark_header_paths) if snap.watermark_header_enabled else []
    footer_paths = _parse_paths(snap.watermark_footer_paths) if snap.watermark_footer_enabled else []

    _wm_run_log(
        console_func,
        "Parsed existing files: "
        f"fullpage={len(v1_paths)}, overlay={len(v2_paths)}, "
        f"header={len(header_paths)}, footer={len(footer_paths)}"
    )

    if snap.watermark_fullpage_enabled and not v1_paths:
        _wm_run_log(console_func, "WARNING: Fullpage watermark is enabled, but no valid file path was found.")
    if snap.watermark_overlay_enabled and not v2_paths:
        _wm_run_log(console_func, "WARNING: Overlay watermark is enabled, but no valid file path was found.")
    if snap.watermark_header_enabled and not header_paths:
        _wm_run_log(console_func, "WARNING: Header is enabled, but no valid file path was found.")
    if snap.watermark_footer_enabled and not footer_paths:
        _wm_run_log(console_func, "WARNING: Footer is enabled, but no valid file path was found.")

    if not (snap.watermark_fullpage_enabled or snap.watermark_overlay_enabled):
        _wm_run_log(console_func, "INFO: Fullpage and overlay are disabled; only header/footer (if enabled) will be applied.")

    if v1_paths or v2_paths:
        wm_service.load_watermarks(v1_paths, v2_paths)
    else:
        _wm_run_log(console_func, "No fullpage/overlay files loaded into WatermarkService.")

    wm_settings = {
        "lossy_quality": snap.lossy_quality,
        "watermark_fullpage_enabled": snap.watermark_fullpage_enabled and bool(v1_paths),
        "watermark_fullpage_position": snap.watermark_fullpage_position,
        "watermark_fullpage_frequency": snap.watermark_fullpage_frequency,
        "watermark_fullpage_threshold": snap.watermark_fullpage_threshold,
        "watermark_fullpage_alternate_interval": snap.watermark_fullpage_alternate_interval,
        "watermark_fullpage_max_per_page": snap.watermark_fullpage_max_per_page,
        "watermark_fullpage_block_strategy": snap.watermark_fullpage_block_strategy,
        "watermark_overlay_enabled": snap.watermark_overlay_enabled and bool(v2_paths),
        "watermark_overlay_position": snap.watermark_overlay_position,
        "watermark_overlay_opacity": snap.watermark_overlay_opacity,
        "watermark_overlay_scale_pct": snap.watermark_overlay_scale_pct,
        "watermark_overlay_max_per_page": snap.watermark_overlay_max_per_page,
        "add_header": snap.watermark_header_enabled,
        "header_images": header_paths,
        "add_footer": snap.watermark_footer_enabled,
        "footer_images": footer_paths,
    }

    _wm_run_log(
        console_func,
        "Effective settings sent to service: "
        f"fullpage_enabled={wm_settings['watermark_fullpage_enabled']}, "
        f"overlay_enabled={wm_settings['watermark_overlay_enabled']}, "
        f"add_header={wm_settings['add_header']}, add_footer={wm_settings['add_footer']}"
    )

    console_func("Applying watermarks...\n")
    try:
        wm_service.process_chapter_folder(output_dir, wm_settings)
    finally:
        wm_service.close_watermarks()
    console_func("Watermarks applied.\n")


def _run_single_directory(
    work_dir,
    snap: _SettingsSnapshot,
    *,
    psd_first_layer_only: bool,
    cancel_event: threading.Event | None = None,
    max_workers: int | None = None,
    console_func: ConsoleFunc = print,
    status_callback: Callable[[str, str], None] | None = None,
) -> tuple[int, dict[str, float], int]:
    """Core image pipeline: load → resize → combine → detect → slice → save.

    This is the unified processing function used by both sequential and parallel modes.
    Returns the number of output images produced.

    Args:
        work_dir: WorkDirectory with input/output paths
        snap: Immutable settings snapshot
        psd_first_layer_only: Whether to use only first PSD layer
        max_workers: Max workers for image operations
        console_func: Function to output console messages
        status_callback: Optional callback(step, message) for progress updates
    """
    def _status(step: str, msg: str) -> None:
        if status_callback:
            status_callback(step, msg)

    img_handler = ImageHandler(max_workers=max_workers)
    img_manipulator = ImageManipulator(max_workers=max_workers)
    detector = select_detector(detection_type=snap.detector_type)

    def _check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Process cancelled due to failure in another directory.")

    sensitivity = snap.sensitivity
    scan_step = snap.scan_step
    ignorable_pixels = snap.ignorable_pixels
    img_count = 0
    retry_count = 0
    stage_seconds: dict[str, float] = {
        "load": 0.0,
        "resize": 0.0,
        "combine": 0.0,
        "detect": 0.0,
        "slice": 0.0,
        "save": 0.0,
    }

    for attempt in range(_MAX_SENSITIVITY_RETRIES + 1):
        imgs = None
        combined_img = None
        sliced = None
        try:
            _check_cancelled()
            _status("load", "Preparing & loading images into memory")
            stage_start = time()
            imgs = img_handler.load(work_dir, psd_first_layer_only=psd_first_layer_only)
            stage_seconds["load"] += time() - stage_start

            stage_start = time()
            imgs = img_manipulator.resize(imgs, snap.enforce_type, snap.enforce_width)
            stage_seconds["resize"] += time() - stage_start

            _check_cancelled()
            _status("combine", "Combining images into a single combined image")
            stage_start = time()
            combined_img = img_manipulator.combine(imgs)
            stage_seconds["combine"] += time() - stage_start

            _check_cancelled()
            _status("detect", "Detecting & selecting valid slicing points")
            stage_start = time()
            slice_points = detector.run(
                combined_img,
                snap.split_height,
                sensitivity=sensitivity,
                ignorable_pixels=ignorable_pixels,
                scan_step=scan_step,
            )
            if snap.output_type.lower() in (".jpg", ".jpeg"):
                slice_points = ensure_max_slice_segment(
                    slice_points,
                    combined_height=combined_img.size[1],
                    max_segment=_MAX_PIL_IMAGE_DIMENSION,
                )
            stage_seconds["detect"] += time() - stage_start

            _check_cancelled()
            _status("slice", "Generating sliced output images in memory")
            stage_start = time()
            sliced = img_manipulator.slice(combined_img, slice_points)
            stage_seconds["slice"] += time() - stage_start

            _check_cancelled()
            _status("save", "Saving output images to storage")
            img_count = len(sliced)
            stage_start = time()
            img_handler.save_all(
                work_dir, sliced, img_format=snap.output_type, quality=snap.lossy_quality,
            )
            stage_seconds["save"] += time() - stage_start
            _status("save", f"{img_count} images saved successfully")
            break

        except Exception as exc:
            if attempt >= _MAX_SENSITIVITY_RETRIES or not is_dimension_error(exc):
                raise

            new_sensitivity = max(0, int(sensitivity * _SENSITIVITY_RETRY_FACTOR))
            retry_count += 1
            console_func(
                f"Retrying folder '{work_dir.input_path}' due to large image output. "
                f"Adjusting sensitivity {sensitivity} → {new_sensitivity}, scan_step → 5, "
                f"ignorable_pixels → 5 (attempt {attempt + 1}/{_MAX_SENSITIVITY_RETRIES}).\n"
            )
            sensitivity = new_sensitivity
            scan_step = 5
            ignorable_pixels = 5

        finally:
            close_images_safely(sliced, combined_img, imgs)

    return img_count, stage_seconds, retry_count


def _run_pipeline(
    work_dir,
    snap: _SettingsSnapshot,
    *,
    psd_first_layer_only: bool,
    cancel_event: threading.Event | None = None,
    has_postprocess: bool,
    run_comiczip: bool,
    max_workers: int | None = None,
    console_func: ConsoleFunc = print,
) -> tuple[int, dict[str, float], int]:
    """Run full pipeline for a single directory (used by parallel mode).

    Returns the number of output images produced.
    """
    img_count, stage_seconds, retry_count = _run_single_directory(
        work_dir, snap,
        psd_first_layer_only=psd_first_layer_only,
        cancel_event=cancel_event,
        max_workers=max_workers,
        console_func=console_func,
    )

    if snap.has_watermark:
        stage_start = time()
        _run_watermark(work_dir.output_path, snap, console_func)
        stage_seconds["watermark"] = stage_seconds.get("watermark", 0.0) + (time() - stage_start)

    postprocess_runner = PostProcessRunner()
    if has_postprocess:
        stage_start = time()
        postprocess_runner.run(
            workdirectory=work_dir,
            postprocess_app=snap.postprocess_app,
            postprocess_args=snap.postprocess_args,
            console_func=console_func,
        )
        stage_seconds["postprocess"] = stage_seconds.get("postprocess", 0.0) + (time() - stage_start)
    if run_comiczip:
        stage_start = time()
        postprocess_runner.run(
            workdirectory=work_dir,
            postprocess_app="python",
            postprocess_args=f"{_COMICZIP_SCRIPT} -i [stitched] -o [processed]",
            console_func=console_func,
        )
        stage_seconds["comiczip"] = stage_seconds.get("comiczip", 0.0) + (time() - stage_start)

    return img_count, stage_seconds, retry_count


def _process_work_directory(
    work_dir,
    snap: _SettingsSnapshot,
    *,
    psd_first_layer_only: bool,
    cancel_event: threading.Event | None = None,
    disable_postprocess: bool,
    disable_comiczip: bool,
    inner_max_workers: int | None = None,
) -> tuple[str, int, dict[str, float], int]:
    """Entry point for parallel (subprocess) execution of a single directory."""
    _wm_run_log(
        print,
        "_process_work_directory using shared snapshot flags: "
        f"fullpage_enabled={snap.watermark_fullpage_enabled}, "
        f"overlay_enabled={snap.watermark_overlay_enabled}, "
        f"header_enabled={snap.watermark_header_enabled}, "
        f"footer_enabled={snap.watermark_footer_enabled}"
    )
    img_count, stage_seconds, retry_count = _run_pipeline(
        work_dir,
        snap,
        psd_first_layer_only=psd_first_layer_only,
        cancel_event=cancel_event,
        has_postprocess=snap.run_postprocess and not disable_postprocess,
        run_comiczip=snap.run_comiczip and not disable_comiczip,
        max_workers=inner_max_workers,
    )
    return work_dir.input_path, img_count, stage_seconds, retry_count


class GuiStitchProcess:
    @logFunc(inclass=True)
    def run_with_error_msgs(self, **kwargs):
        status_func: StatusFunc = kwargs.get("status_func", print)
        try:
            return self.run(**kwargs)
        except Exception as error:
            status_func(0, f"Idle - {error}")
            raise

    def run(self, **kwargs):
        input_path: str = kwargs.get("input_path", "")
        output_path: str = kwargs.get("output_path", "")
        postprocess_path: str = kwargs.get("postprocess_path", "")
        psd_first_layer_only: bool = kwargs.get("psd_first_layer_only", False)
        disable_postprocess: bool = kwargs.get("disable_postprocess", False)
        disable_comiczip: bool = kwargs.get("disable_comiczip", False)
        status_func: StatusFunc = kwargs.get("status_func", print)
        console_func: ConsoleFunc = kwargs.get("console_func", print)

        settings = SettingsHandler()
        snap = _SettingsSnapshot.from_settings(settings)
        wm_line = (
            "GuiStitchProcess.run settings: "
            f"settings_file='{settings.settings_file}', "
            f"fullpage_enabled={snap.watermark_fullpage_enabled}, "
            f"overlay_enabled={snap.watermark_overlay_enabled}, "
            f"header_enabled={snap.watermark_header_enabled}, "
            f"footer_enabled={snap.watermark_footer_enabled}"
        )
        _wm_run_log(console_func, wm_line)
        has_postprocess = snap.run_postprocess and not disable_postprocess
        run_comiczip = snap.run_comiczip and not disable_comiczip
        benchmark = PerfBenchmark(
            mode="gui",
            enabled=is_benchmark_enabled(),
            metadata={
                "input_path": input_path,
                "parallel_processing": bool(snap.parallel_processing),
                "has_postprocess": bool(has_postprocess),
                "run_comiczip": bool(run_comiczip),
            },
        )

        step_pct = {
            "explore": 5.0,
            "load": 15.0,
            "combine": 5.0,
            "detect": 15.0,
            "slice": 10.0,
            "save": 50.0 if not has_postprocess else 30.0,
            "postprocess": 20.0,
        }

        start_time = time()
        pct = 0.0
        status_func(pct, "Exploring input directory for working directories")

        explorer_kwargs: dict[str, str] = {}
        if output_path:
            explorer_kwargs["output"] = output_path
        if postprocess_path:
            explorer_kwargs["postprocess"] = postprocess_path

        input_dirs = DirectoryExplorer().run(input=input_path, **explorer_kwargs)
        total = len(input_dirs)
        status_func(pct, f"Working - [{total}] Working directories were found")
        pct += step_pct["explore"]

        if total > 1 and snap.parallel_processing:
            self._run_parallel(
                input_dirs, total, snap, pct, start_time,
                psd_first_layer_only=psd_first_layer_only,
                disable_postprocess=disable_postprocess,
                disable_comiczip=disable_comiczip,
                status_func=status_func,
                benchmark=benchmark,
            )
            return

        self._run_sequential(
            input_dirs, total, snap, pct, step_pct, start_time,
            psd_first_layer_only=psd_first_layer_only,
            has_postprocess=has_postprocess,
            run_comiczip=run_comiczip,
            status_func=status_func,
            console_func=console_func,
            benchmark=benchmark,
        )

    @staticmethod
    def _run_parallel(
        input_dirs, total: int, snap: _SettingsSnapshot,
        base_pct: float, start_time: float,
        *,
        psd_first_layer_only: bool,
        disable_postprocess: bool,
        disable_comiczip: bool,
        status_func: StatusFunc,
        benchmark: PerfBenchmark,
    ) -> None:
        """Process multiple directories with controlled parallelism.
        
        Uses ThreadPoolExecutor instead of ProcessPoolExecutor to avoid:
        - System instability from too many processes
        - Memory explosion from process spawning
        - Potential system shutdown from resource exhaustion
        
        Limits concurrent directories to _MAX_PARALLEL_DIRECTORIES.
        """
        # Limit parallel workers to prevent system overload
        max_workers = min(total, _MAX_PARALLEL_DIRECTORIES)
        status_func(base_pct, f"Working - Processing {total} directories ({max_workers} at a time)")
        cancel_event = threading.Event()

        completed = 0

        # Use ThreadPoolExecutor - safer than ProcessPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _process_work_directory, d, snap,
                    psd_first_layer_only=psd_first_layer_only,
                    cancel_event=cancel_event,
                    disable_postprocess=disable_postprocess,
                    disable_comiczip=disable_comiczip,
                    inner_max_workers=1,
                ): d
                for d in input_dirs
            }

            for fut in concurrent.futures.as_completed(futures):
                work_dir = futures[fut]
                try:
                    dir_path, img_count, stage_seconds, retry_count = fut.result()
                    completed += 1
                    dirname = os.path.basename(dir_path) or dir_path
                    msg = f"Working - [{completed}/{total}] Done: {dirname} ({img_count} imgs)"
                    benchmark.add_directory(
                        input_path=work_dir.input_path,
                        output_path=work_dir.output_path,
                        image_count=img_count,
                        retries=retry_count,
                        stage_seconds=stage_seconds,
                        success=True,
                    )
                except Exception as exc:
                    cancel_event.set()
                    dirname = os.path.basename(work_dir.input_path) or work_dir.input_path
                    msg = f"Working - Failed: {dirname} -> {exc}"
                    benchmark.add_directory(
                        input_path=work_dir.input_path,
                        output_path=work_dir.output_path,
                        image_count=0,
                        retries=0,
                        stage_seconds={},
                        success=False,
                        error=str(exc),
                    )
                    status_func(int(base_pct), msg)
                    for pending in futures:
                        if pending is not fut:
                            pending.cancel()
                    raise RuntimeError(msg) from exc

                progress = base_pct + (100.0 - base_pct) * (completed / total)
                status_func(int(progress), msg)
                
                # Force garbage collection between directories
                gc.collect()

        elapsed = time() - start_time
        benchmark_file = benchmark.write_json(file_prefix="benchmark", total_elapsed_s=elapsed)
        if benchmark_file:
            status_func(100, f"Idle - Benchmark saved: {benchmark_file}")
        status_func(100, f"Idle - Process completed in {elapsed:.3f} seconds")

    @staticmethod
    def _run_sequential(
        input_dirs, total: int, snap: _SettingsSnapshot,
        pct: float, step_pct: dict[str, float], start_time: float,
        *,
        psd_first_layer_only: bool,
        has_postprocess: bool,
        run_comiczip: bool,
        status_func: StatusFunc,
        console_func: ConsoleFunc,
        benchmark: PerfBenchmark,
    ) -> None:
        postprocess_runner = PostProcessRunner()

        for idx, work_dir in enumerate(input_dirs, 1):
            try:
                per_dir = 1.0 / total

                def _status_callback(step: str, msg: str) -> None:
                    status_func(pct, f"Working - [{idx}/{total}] {msg}")

                img_count, stage_seconds, retry_count = _run_single_directory(
                    work_dir, snap,
                    psd_first_layer_only=psd_first_layer_only,
                    console_func=console_func,
                    status_callback=_status_callback,
                )
                pct += (step_pct["load"] + step_pct["combine"] + step_pct["detect"] +
                        step_pct["slice"] + step_pct["save"]) * per_dir

                if snap.has_watermark:
                    status_func(pct, f"Working - [{idx}/{total}] Applying watermarks")
                    stage_start = time()
                    _run_watermark(work_dir.output_path, snap, console_func)
                    stage_seconds["watermark"] = stage_seconds.get("watermark", 0.0) + (time() - stage_start)

                gc.collect()

                if has_postprocess:
                    status_func(pct, f"Working - [{idx}/{total}] Running post process on output files")
                    stage_start = time()
                    postprocess_runner.run(
                        workdirectory=work_dir,
                        postprocess_app=snap.postprocess_app,
                        postprocess_args=snap.postprocess_args,
                        console_func=console_func,
                    )
                    stage_seconds["postprocess"] = stage_seconds.get("postprocess", 0.0) + (time() - stage_start)
                    pct += step_pct["postprocess"] * per_dir

                if run_comiczip:
                    status_func(pct, f"Working - [{idx}/{total}] Running ComicZip on output files")
                    stage_start = time()
                    postprocess_runner.run(
                        workdirectory=work_dir,
                        postprocess_app="python",
                        postprocess_args=f"{_COMICZIP_SCRIPT} -i [stitched] -o [processed]",
                        console_func=console_func,
                    )
                    stage_seconds["comiczip"] = stage_seconds.get("comiczip", 0.0) + (time() - stage_start)

                benchmark.add_directory(
                    input_path=work_dir.input_path,
                    output_path=work_dir.output_path,
                    image_count=img_count,
                    retries=retry_count,
                    stage_seconds=stage_seconds,
                    success=True,
                )

            except Exception as exc:
                benchmark.add_directory(
                    input_path=work_dir.input_path,
                    output_path=work_dir.output_path,
                    image_count=0,
                    retries=0,
                    stage_seconds={},
                    success=False,
                    error=str(exc),
                )
                status_func(int(pct), f"Working - [{idx}/{total}] Failed: {exc}")
                raise

        elapsed = time() - start_time
        benchmark_file = benchmark.write_json(file_prefix="benchmark", total_elapsed_s=elapsed)
        if benchmark_file:
            console_func(f"Benchmark saved: {benchmark_file}\n")
        status_func(100, f"Idle - Process completed in {elapsed:.3f} seconds")
