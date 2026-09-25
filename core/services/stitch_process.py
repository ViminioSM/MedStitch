"""Unified stitch process shared by GUI and console (bot-friendly CLI)."""

import concurrent.futures
import gc
import os
import threading
from dataclasses import dataclass, fields, replace
from time import time
from typing import Any, Callable

from core.models.app_settings import AppSettings
from core.services.comiczip_service import ComicZipService
from core.services.directory_explorer import DirectoryExplorer
from core.services.global_logger import logFunc
from core.services.image_handler import ImageHandler
from core.services.image_manipulator import ImageManipulator
from core.services.perf_benchmark import PerfBenchmark, is_benchmark_enabled
from core.services.postprocess_runner import PostProcessRunner
from core.services.settings_handler import SettingsHandler
from core.services.watermark_service import WatermarkService
from core.utils.image_utils import (
    _MAX_PIL_IMAGE_DIMENSION,
    _MAX_SENSITIVITY_RETRIES,
    _SENSITIVITY_RETRY_FACTOR,
    close_images_safely,
    ensure_max_slice_segment,
    is_dimension_error,
    max_slice_segment_for_format,
)

# Parallel processing limits based on CPU cores (no artificial cap)
_MAX_PARALLEL_DIRECTORIES = max(4, (os.cpu_count() or 4) * 2)
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
    except Exception:  # nosec B110
        pass
    print(line)


@dataclass(frozen=True)
class SettingsSnapshot:
    """Immutable snapshot of all settings needed during a processing run.

    Loaded once to avoid repeated disk/JSON reads inside tight loops.
    """

    split_height: int
    output_type: str
    extra_output_types: list
    lossy_quality: int
    enforce_type: int
    enforce_width: int
    detector_type: int
    sensitivity: int
    ignorable_pixels: int
    scan_step: int
    run_postprocess: bool
    postprocess_compact_enabled: bool
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
    watermark_overlay_margin: int
    watermark_header_enabled: bool
    watermark_header_paths: str
    watermark_footer_enabled: bool
    watermark_footer_paths: str
    watermark_fullpage_max_per_page: int
    watermark_fullpage_block_strategy: int
    watermark_fullpage_insert_mode: bool
    watermark_fullpage_min_area_height: int
    watermark_fullpage_min_spacing_top: int
    watermark_fullpage_min_spacing_bottom: int
    watermark_fullpage_min_spacing_sides: int
    watermark_fullpage_require_centered_space: bool
    watermark_chapter_skip: int

    @property
    def has_watermark(self) -> bool:
        return (
            (
                self.watermark_fullpage_enabled
                and bool(self.watermark_fullpage_paths.strip())
            )
            or (
                self.watermark_overlay_enabled
                and bool(self.watermark_overlay_paths.strip())
            )
            or (
                self.watermark_header_enabled
                and bool(self.watermark_header_paths.strip())
            )
            or (
                self.watermark_footer_enabled
                and bool(self.watermark_footer_paths.strip())
            )
        )

    @classmethod
    def field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}

    def with_overrides(self, overrides: dict[str, Any] | None) -> "SettingsSnapshot":
        if not overrides:
            return self
        valid = self.field_names()
        filtered = {k: v for k, v in overrides.items() if k in valid and v is not None}
        if not filtered:
            return self
        return replace(self, **filtered)

    @classmethod
    def from_mapping(
        cls,
        mapping: dict[str, Any] | AppSettings | SettingsHandler,
        *,
        overrides: dict[str, Any] | None = None,
    ) -> "SettingsSnapshot":
        """Build snapshot from AppSettings, SettingsHandler, or plain dict."""
        if isinstance(mapping, SettingsHandler):
            data = {name: mapping.load(name) for name in cls.field_names()}
        elif isinstance(mapping, AppSettings):
            data = {name: getattr(mapping, name) for name in cls.field_names()}
        else:
            defaults = AppSettings()
            data = {name: getattr(defaults, name) for name in cls.field_names()}
            for name in cls.field_names():
                if name in mapping:
                    data[name] = mapping[name]
        snap = cls(**data)
        return snap.with_overrides(overrides)

    @classmethod
    def from_settings(
        cls,
        s: SettingsHandler,
        *,
        overrides: dict[str, Any] | None = None,
    ) -> "SettingsSnapshot":
        return cls.from_mapping(s, overrides=overrides)

    @classmethod
    def from_defaults(
        cls, *, overrides: dict[str, Any] | None = None
    ) -> "SettingsSnapshot":
        return cls.from_mapping(AppSettings(), overrides=overrides)


# Backward-compatible private alias
_SettingsSnapshot = SettingsSnapshot


def _parse_paths(raw: str) -> list[str]:
    """Split a semicolon-separated path string into a list of existing paths."""
    return [
        p.strip() for p in raw.split(";") if p.strip() and os.path.isfile(p.strip())
    ]


def _split_output_types(raw: str | list | None) -> list[str]:
    """Split an output_type value into individual extensions.

    Accepts ".webp", ".webp+.png" or a list like [".webp", ".png"].
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        parts = [str(p or "").strip().lower() for p in raw]
    else:
        parts = str(raw or "").replace(";", "+").replace(",", "+").split("+")
        parts = [p.strip().lower() for p in parts]
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if not part.startswith("."):
            part = f".{part}"
        if part not in out:
            out.append(part)
    return out


def _primary_output_format(raw: str | list | None) -> str:
    parts = _split_output_types(raw)
    return parts[0] if parts else ".png"


def _resolve_extra_formats(raw: str | list | None, extra: list | None) -> list[str]:
    """Extra formats to save alongside the primary (dual output support).

    ``output_type=".webp+.png"`` implies extra ``[".png"]`` automatically.
    Any explicit ``extra_output_types`` list is merged in (primary excluded).
    """
    parts = _split_output_types(raw)
    merged: list[str] = list(parts[1:])
    for item in extra or []:
        ext = str(item or "").strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        if ext and ext not in merged:
            merged.append(ext)
    primary = parts[0] if parts else ""
    return [e for e in merged if e and e != primary]


def _watermark_inline(
    sliced: list,
    snap: _SettingsSnapshot,
    console_func: ConsoleFunc = print,
) -> tuple[list, dict[str, float], dict[str, int | bool]]:
    """Apply watermarks to in-memory PIL images before saving."""
    wm_stage_seconds: dict[str, float] = {
        "watermark_prepare_assets": 0.0,
        "watermark_apply_images": 0.0,
        "watermark_release_assets": 0.0,
        "watermark_total": 0.0,
    }
    wm_details: dict[str, int | bool] = {
        "fullpage_active": False,
        "overlay_active": False,
        "header_active": False,
        "footer_active": False,
        "fullpage_assets": 0,
        "overlay_assets": 0,
        "header_assets": 0,
        "footer_assets": 0,
    }

    if not snap.has_watermark or not sliced:
        return sliced, wm_stage_seconds, wm_details

    prep_started = time()
    wm_service = WatermarkService()
    v1_paths = (
        _parse_paths(snap.watermark_fullpage_paths)
        if snap.watermark_fullpage_enabled
        else []
    )
    v2_paths = (
        _parse_paths(snap.watermark_overlay_paths)
        if snap.watermark_overlay_enabled
        else []
    )
    header_paths = (
        _parse_paths(snap.watermark_header_paths)
        if snap.watermark_header_enabled
        else []
    )
    footer_paths = (
        _parse_paths(snap.watermark_footer_paths)
        if snap.watermark_footer_enabled
        else []
    )

    wm_details["fullpage_active"] = bool(snap.watermark_fullpage_enabled and v1_paths)
    wm_details["overlay_active"] = bool(snap.watermark_overlay_enabled and v2_paths)
    wm_details["header_active"] = bool(snap.watermark_header_enabled and header_paths)
    wm_details["footer_active"] = bool(snap.watermark_footer_enabled and footer_paths)
    wm_details["fullpage_assets"] = len(v1_paths)
    wm_details["overlay_assets"] = len(v2_paths)
    wm_details["header_assets"] = len(header_paths)
    wm_details["footer_assets"] = len(footer_paths)

    if v1_paths or v2_paths:
        wm_service.load_watermarks(v1_paths, v2_paths)
    wm_stage_seconds["watermark_prepare_assets"] = time() - prep_started

    if not (v1_paths or v2_paths or header_paths or footer_paths):
        wm_service.close_watermarks()
        return sliced, wm_stage_seconds, wm_details

    wm_settings = {
        "lossy_quality": snap.lossy_quality,
        "watermark_fullpage_enabled": snap.watermark_fullpage_enabled and bool(v1_paths),
        "watermark_fullpage_position": snap.watermark_fullpage_position,
        "watermark_fullpage_frequency": snap.watermark_fullpage_frequency,
        "watermark_fullpage_threshold": snap.watermark_fullpage_threshold,
        "watermark_fullpage_alternate_interval": snap.watermark_fullpage_alternate_interval,
        "watermark_fullpage_max_per_page": snap.watermark_fullpage_max_per_page,
        "watermark_fullpage_block_strategy": snap.watermark_fullpage_block_strategy,
        "watermark_fullpage_insert_mode": snap.watermark_fullpage_insert_mode,
        "watermark_fullpage_min_area_height": snap.watermark_fullpage_min_area_height,
        "watermark_fullpage_min_spacing_top": snap.watermark_fullpage_min_spacing_top,
        "watermark_fullpage_min_spacing_bottom": snap.watermark_fullpage_min_spacing_bottom,
        "watermark_fullpage_min_spacing_sides": snap.watermark_fullpage_min_spacing_sides,
        "watermark_fullpage_require_centered_space": snap.watermark_fullpage_require_centered_space,
        "watermark_chapter_skip": snap.watermark_chapter_skip,
        "watermark_overlay_enabled": snap.watermark_overlay_enabled and bool(v2_paths),
        "watermark_overlay_position": snap.watermark_overlay_position,
        "watermark_overlay_opacity": snap.watermark_overlay_opacity,
        "watermark_overlay_scale_pct": snap.watermark_overlay_scale_pct,
        "watermark_overlay_max_per_page": snap.watermark_overlay_max_per_page,
        "watermark_overlay_margin": snap.watermark_overlay_margin,
        "add_header": snap.watermark_header_enabled,
        "header_images": header_paths,
        "add_footer": snap.watermark_footer_enabled,
        "footer_images": footer_paths,
    }

    total = len(sliced)
    chapter_skip = int(wm_settings.get("watermark_chapter_skip", 0))
    try:
        apply_started = time()
        for i, img in enumerate(sliced):
            if chapter_skip > 1 and (i + 1) % chapter_skip != 0:
                continue
            is_first = (i == 0)
            is_last = (i == total - 1)
            result = wm_service.watermark_image(
                img,
                wm_settings,
                is_first=is_first,
                is_last=is_last,
            )
            if result is not None:
                img.close()
                sliced[i] = result
        wm_stage_seconds["watermark_apply_images"] = time() - apply_started
    finally:
        release_started = time()
        wm_service.close_watermarks()
        wm_stage_seconds["watermark_release_assets"] = time() - release_started

    wm_stage_seconds["watermark_total"] = (
        wm_stage_seconds["watermark_prepare_assets"]
        + wm_stage_seconds["watermark_apply_images"]
        + wm_stage_seconds["watermark_release_assets"]
    )

    return sliced, wm_stage_seconds, wm_details


def _run_single_directory(
    work_dir,
    snap: _SettingsSnapshot,
    *,
    psd_first_layer_only: bool,
    cancel_event: threading.Event | None = None,
    max_workers: int | None = None,
    console_func: ConsoleFunc = print,
    status_callback: Callable[[str, str], None] | None = None,
) -> tuple[int, dict[str, float], int, dict[str, int | bool]]:
    """Core image pipeline: load → resize → combine → detect → slice → watermark → save.

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
    img_manipulator = ImageManipulator()
    from core.detectors import select_detector
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
            primary_fmt = _primary_output_format(snap.output_type)
            # Lossless safety net for very large chapters: no slice may exceed
            # the encoder limit, regardless of format. This only inserts extra
            # cut points (never recompresses), so quality is untouched — and it
            # avoids the expensive 4x full-pipeline retry on dimension errors.
            slice_points = ensure_max_slice_segment(
                slice_points,
                combined_height=combined_img.size[1],
                max_segment=max_slice_segment_for_format(primary_fmt),
            )
            stage_seconds["detect"] += time() - stage_start

            _check_cancelled()
            _status("slice", "Generating sliced output images in memory")
            stage_start = time()
            sliced = img_manipulator.slice(combined_img, slice_points)
            stage_seconds["slice"] += time() - stage_start

            if snap.has_watermark:
                _check_cancelled()
                _status("watermark", "Applying watermarks in memory")
                stage_start = time()
                sliced, wm_stage, wm_details = _watermark_inline(
                    sliced, snap, console_func
                )
                for key, value in wm_stage.items():
                    stage_seconds[key] = stage_seconds.get(key, 0.0) + value
            else:
                wm_details = {
                    "fullpage_active": False,
                    "overlay_active": False,
                    "header_active": False,
                    "footer_active": False,
                    "fullpage_assets": 0,
                    "overlay_assets": 0,
                    "header_assets": 0,
                    "footer_assets": 0,
                }

            if snap.postprocess_compact_enabled:
                _check_cancelled()
                _status("compact", "Compacting top white areas (> 400px)")
                stage_start = time()
                sliced = img_manipulator.compact_top_white(sliced, min_white_height=400)
                stage_seconds["compact"] = stage_seconds.get("compact", 0.0) + (
                    time() - stage_start
                )

            _check_cancelled()
            _status("save", "Saving output images to storage")
            img_count = len(sliced)
            stage_start = time()
            # Dual WEBP+PNG: process (load/resize/combine/detect/slice) ran once;
            # only the final encode is duplicated (primary + extra formats).
            extras = _resolve_extra_formats(snap.output_type, snap.extra_output_types)
            img_handler.save_all(
                work_dir,
                sliced,
                img_format=_primary_output_format(snap.output_type),
                quality=snap.lossy_quality,
                extra_formats=extras,
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

    return img_count, stage_seconds, retry_count, wm_details


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
    postprocess_runner: PostProcessRunner | None = None,
) -> tuple[int, dict[str, float], int, dict[str, int | bool]]:
    """Run full pipeline for a single directory."""
    img_count, stage_seconds, retry_count, wm_details = _run_single_directory(
        work_dir,
        snap,
        psd_first_layer_only=psd_first_layer_only,
        cancel_event=cancel_event,
        max_workers=max_workers,
        console_func=console_func,
    )

    if has_postprocess:
        stage_start = time()
        if postprocess_runner is not None:
            postprocess_runner.run_async_bg(
                workdirectory=work_dir,
                postprocess_app=snap.postprocess_app,
                postprocess_args=snap.postprocess_args,
                console_func=console_func,
            )
        else:
            PostProcessRunner().run(
                workdirectory=work_dir,
                postprocess_app=snap.postprocess_app,
                postprocess_args=snap.postprocess_args,
                console_func=console_func,
            )
        stage_seconds["postprocess"] = stage_seconds.get("postprocess", 0.0) + (
            time() - stage_start
        )
    if run_comiczip:
        stage_start = time()
        ComicZipService().compress_input(
            work_dir.output_path,
            work_dir.postprocess_path,
            console_func=console_func,
        )
        stage_seconds["comiczip"] = stage_seconds.get("comiczip", 0.0) + (
            time() - stage_start
        )

    return img_count, stage_seconds, retry_count, wm_details


def _process_work_directory(
    work_dir,
    snap: _SettingsSnapshot,
    *,
    psd_first_layer_only: bool,
    cancel_event: threading.Event | None = None,
    disable_postprocess: bool,
    disable_comiczip: bool,
    inner_max_workers: int | None = None,
) -> tuple[str, int, dict[str, float], int, dict[str, int | bool]]:
    """Entry point for parallel (subprocess) execution of a single directory."""
    _wm_run_log(
        print,
        "_process_work_directory using shared snapshot flags: "
        f"fullpage_enabled={snap.watermark_fullpage_enabled}, "
        f"overlay_enabled={snap.watermark_overlay_enabled}, "
        f"header_enabled={snap.watermark_header_enabled}, "
        f"footer_enabled={snap.watermark_footer_enabled}",
    )
    img_count, stage_seconds, retry_count, wm_details = _run_pipeline(
        work_dir,
        snap,
        psd_first_layer_only=psd_first_layer_only,
        cancel_event=cancel_event,
        has_postprocess=snap.run_postprocess and not disable_postprocess,
        run_comiczip=snap.run_comiczip and not disable_comiczip,
        max_workers=inner_max_workers,
    )
    return work_dir.input_path, img_count, stage_seconds, retry_count, wm_details


class StitchProcess:
    """Full stitch pipeline used by GUI and console."""

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
        use_saved_settings: bool = kwargs.get("use_saved_settings", True)
        settings_overrides: dict[str, Any] | None = kwargs.get("settings_overrides")
        mode: str = kwargs.get("mode", "gui")

        settings_file = ""
        if use_saved_settings:
            settings = SettingsHandler()
            settings_file = settings.settings_file
            snap = SettingsSnapshot.from_settings(
                settings, overrides=settings_overrides
            )
        else:
            snap = SettingsSnapshot.from_defaults(overrides=settings_overrides)

        wm_line = (
            f"StitchProcess.run mode={mode}: "
            f"settings_file='{settings_file or '<defaults>'}', "
            f"fullpage_enabled={snap.watermark_fullpage_enabled}, "
            f"overlay_enabled={snap.watermark_overlay_enabled}, "
            f"header_enabled={snap.watermark_header_enabled}, "
            f"footer_enabled={snap.watermark_footer_enabled}"
        )
        _wm_run_log(console_func, wm_line)
        has_postprocess = snap.run_postprocess and not disable_postprocess
        run_comiczip = snap.run_comiczip and not disable_comiczip
        benchmark = PerfBenchmark(
            mode=mode,
            enabled=is_benchmark_enabled(),
            metadata={
                "input_path": input_path,
                "parallel_processing": bool(snap.parallel_processing),
                "has_postprocess": bool(has_postprocess),
                "run_comiczip": bool(run_comiczip),
                "use_saved_settings": bool(use_saved_settings),
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
                input_dirs,
                total,
                snap,
                pct,
                start_time,
                psd_first_layer_only=psd_first_layer_only,
                disable_postprocess=disable_postprocess,
                disable_comiczip=disable_comiczip,
                status_func=status_func,
                benchmark=benchmark,
            )
            return

        self._run_sequential(
            input_dirs,
            total,
            snap,
            pct,
            step_pct,
            start_time,
            psd_first_layer_only=psd_first_layer_only,
            has_postprocess=has_postprocess,
            run_comiczip=run_comiczip,
            status_func=status_func,
            console_func=console_func,
            benchmark=benchmark,
        )

    @staticmethod
    def _run_parallel(
        input_dirs,
        total: int,
        snap: _SettingsSnapshot,
        base_pct: float,
        start_time: float,
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
        status_func(
            base_pct,
            f"Working - Processing {total} directories ({max_workers} at a time)",
        )
        cancel_event = threading.Event()

        completed = 0

        # Adaptive inner parallelism (identical pixels, only scheduling changes):
        # a single huge chapter gets full load/save workers; many directories
        # keep inner=1 to avoid N_gigantic_images x workers RAM explosion.
        adaptive_inner: int | None = None if total <= 1 else 1
        # Use ThreadPoolExecutor - safer than ProcessPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _process_work_directory,
                    d,
                    snap,
                    psd_first_layer_only=psd_first_layer_only,
                    cancel_event=cancel_event,
                    disable_postprocess=disable_postprocess,
                    disable_comiczip=disable_comiczip,
                    inner_max_workers=adaptive_inner,
                ): d
                for d in input_dirs
            }

            for fut in concurrent.futures.as_completed(futures):
                work_dir = futures[fut]
                try:
                    dir_path, img_count, stage_seconds, retry_count, wm_details = (
                        fut.result()
                    )
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
                        details={"watermark": wm_details},
                    )
                except Exception as exc:
                    cancel_event.set()
                    dirname = (
                        os.path.basename(work_dir.input_path) or work_dir.input_path
                    )
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
        benchmark_file = benchmark.write_json(
            file_prefix="benchmark", total_elapsed_s=elapsed
        )
        if benchmark_file:
            status_func(100, f"Idle - Benchmark saved: {benchmark_file}")
        status_func(100, f"Idle - Process completed in {elapsed:.3f} seconds")

    @staticmethod
    def _run_sequential(
        input_dirs,
        total: int,
        snap: _SettingsSnapshot,
        pct: float,
        step_pct: dict[str, float],
        start_time: float,
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

                def _status_callback(
                    step: str, msg: str, _pct: float = pct, _idx: int = idx
                ) -> None:
                    status_func(_pct, f"Working - [{_idx}/{total}] {msg}")

                img_count, stage_seconds, retry_count, wm_details = _run_single_directory(
                    work_dir,
                    snap,
                    psd_first_layer_only=psd_first_layer_only,
                    console_func=console_func,
                    status_callback=_status_callback,
                )
                pct += (
                    step_pct["load"]
                    + step_pct["combine"]
                    + step_pct["detect"]
                    + step_pct["slice"]
                    + step_pct["save"]
                ) * per_dir

                gc.collect()

                if has_postprocess:
                    status_func(
                        pct,
                        f"Working - [{idx}/{total}] Launching post process",
                    )
                    stage_start = time()
                    postprocess_runner.run_async_bg(
                        workdirectory=work_dir,
                        postprocess_app=snap.postprocess_app,
                        postprocess_args=snap.postprocess_args,
                        console_func=console_func,
                    )
                    stage_seconds["postprocess"] = stage_seconds.get(
                        "postprocess", 0.0
                    ) + (time() - stage_start)
                    pct += step_pct["postprocess"] * per_dir

                if run_comiczip:
                    status_func(
                        pct,
                        f"Working - [{idx}/{total}] Running ComicZip on output files",
                    )
                    stage_start = time()
                    ComicZipService().compress_input(
                        work_dir.output_path,
                        work_dir.postprocess_path,
                        console_func=console_func,
                    )
                    stage_seconds["comiczip"] = stage_seconds.get("comiczip", 0.0) + (
                        time() - stage_start
                    )

                benchmark.add_directory(
                    input_path=work_dir.input_path,
                    output_path=work_dir.output_path,
                    image_count=img_count,
                    retries=retry_count,
                    stage_seconds=stage_seconds,
                    success=True,
                    details={"watermark": wm_details},
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

        postprocess_runner.wait_all(console_func)

        elapsed = time() - start_time
        benchmark_file = benchmark.write_json(
            file_prefix="benchmark", total_elapsed_s=elapsed
        )
        if benchmark_file:
            console_func(f"Benchmark saved: {benchmark_file}\n")
        status_func(100, f"Idle - Process completed in {elapsed:.3f} seconds")


# Backward-compatible alias for GUI / context menu imports
GuiStitchProcess = StitchProcess
