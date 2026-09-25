"""Console CLI launcher with full feature parity for bots and automation."""

from __future__ import annotations

import argparse
import sys

from console.process import ConsoleStitchProcess


def positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"{value} is an invalid positive int value")
    return ivalue


def non_negative_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"{value} is an invalid non-negative int value")
    return ivalue


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="SmartStitchConsole",
        description=(
            "SmartStitch / MedStitch headless stitcher. "
            "Full feature parity with the GUI for bots and automation. "
            "Prefer --config JSON for complex jobs."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ── Paths ──────────────────────────────────────────────────────────────
    paths = parser.add_argument_group("paths")
    paths.add_argument(
        "-i",
        "--input",
        dest="input_folder",
        type=str,
        default=None,
        help="Input folder (required unless provided in --config)",
    )
    paths.add_argument(
        "--from-zip",
        dest="from_zip",
        action="store_true",
        default=False,
        help="Input is an archive (.zip .cbz .rar .7z) — extract first before processing",
    )
    paths.add_argument(
        "--zip-cleanup",
        "--cleanup",
        dest="zip_cleanup",
        action="store_true",
        default=False,
        help="After processing, zip [processed] output as .zip and delete intermediate folders",
    )
    paths.add_argument(
        "-o",
        "--output",
        dest="output_path",
        type=str,
        default=None,
        help="Output folder (default: '<input> [stitched]')",
    )
    paths.add_argument(
        "--postprocess-path",
        dest="postprocess_path",
        type=str,
        default=None,
        help="Postprocess / ComicZip folder (default: '<input> [processed]')",
    )
    paths.add_argument(
        "-c",
        "--config",
        dest="config",
        type=str,
        default=None,
        help="JSON config file. CLI flags override JSON values.",
    )

    # ── Core stitch ────────────────────────────────────────────────────────
    core = parser.add_argument_group("stitch")
    core.add_argument(
        "-sh",
        "--split-height",
        dest="split_height",
        type=positive_int,
        default=None,
        help="Target panel / slice height in pixels",
    )
    core.add_argument(
        "-t",
        "--output-type",
        dest="output_type",
        type=str,
        default=None,
        choices=[".png", ".avif", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tga", ".psd",
                 ".webp+.png", "webp+png", "dual"],
        help="Output image format (.webp+.png/dual = process once, save WEBP+PNG)",
    )
    core.add_argument(
        "--extra-output",
        dest="extra_output_types",
        type=str,
        default=None,
        help="Extra formats saved alongside primary (comma-separated, e.g. .png). "
        "Sliced images are copied, processing is NOT rerun.",
    )
    core.add_argument(
        "-lq",
        "--lossy-quality",
        dest="lossy_quality",
        type=int,
        default=None,
        choices=range(1, 101),
        metavar="[1-100]",
        help="Quality (1-100) for lossy formats (.jpg/.webp/.avif).",
    )
    core.add_argument(
        "-dt",
        "--detection-type",
        dest="detection_type",
        type=str,
        default=None,
        choices=["none", "pixel"],
        help="Slice detection: none=fixed height, pixel=smart bands",
    )
    core.add_argument(
        "-s",
        "--sensitivity",
        dest="detection_sensitivity",
        type=int,
        default=None,
        choices=range(0, 101),
        metavar="[0-100]",
        help="Pixel detection sensitivity (higher = stricter)",
    )
    core.add_argument(
        "-ip",
        "--ignorable-pixels",
        dest="ignorable_pixels",
        type=non_negative_int,
        default=None,
        help="Border pixels ignored during detection",
    )
    core.add_argument(
        "-sl",
        "--scan-step",
        dest="scan_line_step",
        type=int,
        default=None,
        choices=range(1, 100),
        metavar="[1-100]",
        help="Scan step in pixels for smart detection",
    )
    core.add_argument(
        "--psd-first-layer",
        dest="psd_first_layer_only",
        action="store_true",
        default=None,
        help="When loading PSD/PSB, use only the first layer",
    )

    # ── Width enforce ──────────────────────────────────────────────────────
    width = parser.add_argument_group("width enforcement")
    width.add_argument(
        "-cw",
        "--custom-width",
        dest="custom_width",
        type=int,
        default=None,
        help="Force output width (implies enforce=manual). 0/negative disables.",
    )
    width.add_argument(
        "--enforce-type",
        dest="enforce_type",
        type=str,
        default=None,
        choices=["none", "auto", "manual"],
        help="Width enforcement mode",
    )
    width.add_argument(
        "--enforce-width",
        dest="enforce_width",
        type=positive_int,
        default=None,
        help="Target width when enforce-type=manual",
    )

    # ── Pipeline extras ────────────────────────────────────────────────────
    pipeline = parser.add_argument_group("pipeline")
    pipeline.add_argument(
        "--parallel",
        dest="parallel_processing",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Process multiple chapter folders in parallel",
    )
    pipeline.add_argument(
        "--comiczip",
        dest="run_comiczip",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Zip stitched output into ComicZip archive",
    )
    pipeline.add_argument(
        "--compact",
        dest="postprocess_compact_enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Remove large top white areas (>400px) before save",
    )
    pipeline.add_argument(
        "--postprocess",
        dest="run_postprocess",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run external postprocess app after stitch",
    )
    pipeline.add_argument(
        "--postprocess-app",
        dest="postprocess_app",
        type=str,
        default=None,
        help="External postprocess executable (path or PATH name)",
    )
    pipeline.add_argument(
        "--postprocess-args",
        dest="postprocess_args",
        type=str,
        default=None,
        help="Args for postprocess app. Tokens: [stitched] [processed]",
    )
    pipeline.add_argument(
        "--waifu",
        nargs="?",
        const="auto",
        default=None,
        choices=["auto", "jpg", "webp"],
        help=(
            "Enable Waifu2X postprocess (same defaults as GUI). "
            "Optional format: auto (from -t: .webp→webp else jpg), jpg, or webp. "
            "Sets run_postprocess + app C:/Manhwa/Waifu2X/waifu2x-ncnn-vulkan.exe + args. "
            "If the exe is missing, downloads Waifu2X automatically (same as GUI install button). "
            "Override with --postprocess-app / --postprocess-args."
        ),
    )
    pipeline.add_argument(
        "--waifu-repair",
        dest="waifu_repair",
        action="store_true",
        default=False,
        help="Force re-download/repair of Waifu2X before processing (with --waifu).",
    )

    # ── Watermark chapter-level ─────────────────────────────────────────────
    wm_ch = parser.add_argument_group("watermark chapter")
    wm_ch.add_argument(
        "--wm-chapter-skip",
        dest="watermark_chapter_skip",
        type=int,
        default=None,
        help="Apply watermark every N pages (0=all). Ex: 3=pages 1,4,7,10...",
    )

    # ── Watermark fullpage ─────────────────────────────────────────────────
    wm_fp = parser.add_argument_group("watermark fullpage")
    wm_fp.add_argument(
        "--wm-fullpage",
        dest="watermark_fullpage_enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable fullpage watermark",
    )
    wm_fp.add_argument(
        "--wm-fullpage-paths",
        dest="watermark_fullpage_paths",
        nargs="+",
        default=None,
        help="Fullpage watermark image path(s)",
    )
    wm_fp.add_argument(
        "--wm-fullpage-position",
        dest="watermark_fullpage_position",
        choices=["top", "center", "bottom"],
        default=None,
        help="Fullpage watermark vertical position",
    )
    wm_fp.add_argument(
        "--wm-fullpage-frequency",
        dest="watermark_fullpage_frequency",
        choices=["once", "all", "alternating"],
        default=None,
        help="How often fullpage watermark is applied per page",
    )
    wm_fp.add_argument(
        "--wm-fullpage-max",
        dest="watermark_fullpage_max_per_page",
        type=positive_int,
        default=None,
        help="Max fullpage watermarks per page",
    )
    wm_fp.add_argument(
        "--wm-fullpage-strategy",
        dest="watermark_fullpage_block_strategy",
        choices=["first", "best", "random"],
        default=None,
        help="Block selection strategy",
    )
    wm_fp.add_argument(
        "--wm-fullpage-insert",
        dest="watermark_fullpage_insert_mode",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Insert watermark by expanding the page (vs overlay paste)",
    )
    wm_fp.add_argument(
        "--wm-fullpage-min-area",
        dest="watermark_fullpage_min_area_height",
        type=positive_int,
        default=None,
        help="Minimum uniform block height to accept",
    )
    wm_fp.add_argument(
        "--wm-fullpage-alt-interval",
        dest="watermark_fullpage_alternate_interval",
        type=positive_int,
        default=None,
        help="Interval for alternating frequency",
    )

    # ── Watermark overlay ──────────────────────────────────────────────────
    wm_ov = parser.add_argument_group("watermark overlay")
    wm_ov.add_argument(
        "--wm-overlay",
        dest="watermark_overlay_enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable overlay watermark",
    )
    wm_ov.add_argument(
        "--wm-overlay-paths",
        dest="watermark_overlay_paths",
        nargs="+",
        default=None,
        help="Overlay watermark image path(s)",
    )
    wm_ov.add_argument(
        "--wm-overlay-position",
        dest="watermark_overlay_position",
        choices=["auto", "top_left", "top_right", "bottom_left", "bottom_right", "center"],
        default=None,
        help="Overlay position",
    )
    wm_ov.add_argument(
        "--wm-overlay-opacity",
        dest="watermark_overlay_opacity",
        type=int,
        default=None,
        choices=range(0, 101),
        metavar="[0-100]",
        help="Overlay opacity percent",
    )
    wm_ov.add_argument(
        "--wm-overlay-scale",
        dest="watermark_overlay_scale_pct",
        type=int,
        default=None,
        choices=range(5, 101),
        metavar="[5-100]",
        help="Overlay scale as percent of page width",
    )
    wm_ov.add_argument(
        "--wm-overlay-max",
        dest="watermark_overlay_max_per_page",
        type=positive_int,
        default=None,
        help="Max overlay watermarks per page",
    )
    wm_ov.add_argument(
        "--wm-overlay-margin",
        dest="watermark_overlay_margin",
        type=non_negative_int,
        default=None,
        help="Overlay margin from edges",
    )

    # ── Header / footer ────────────────────────────────────────────────────
    hf = parser.add_argument_group("header / footer")
    hf.add_argument(
        "--header",
        dest="watermark_header_enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable header image on first page",
    )
    hf.add_argument(
        "--header-paths",
        dest="watermark_header_paths",
        nargs="+",
        default=None,
        help="Header image path(s)",
    )
    hf.add_argument(
        "--footer",
        dest="watermark_footer_enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable footer image on last page",
    )
    hf.add_argument(
        "--footer-paths",
        dest="watermark_footer_paths",
        nargs="+",
        default=None,
        help="Footer image path(s)",
    )

    # ── Runtime ────────────────────────────────────────────────────────────
    runtime = parser.add_argument_group("runtime")
    runtime.add_argument(
        "--use-saved-settings",
        dest="use_saved_settings",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Start from saved GUI settings profile, then apply CLI/JSON overrides",
    )
    runtime.add_argument(
        "-q",
        "--quiet",
        dest="quiet",
        action="store_true",
        default=False,
        help="Suppress progress output (bots still get exit code)",
    )

    return parser


def launch(argv: list[str] | None = None) -> int:
    import os
    import shutil
    import zipfile

    parser = _build_parser()
    args = parser.parse_args(argv)
    kwargs = vars(args)

    # Require input via CLI or config
    if not kwargs.get("input_folder") and not kwargs.get("config"):
        parser.error("the following arguments are required: -i/--input (or --config with input_folder)")

    from_zip = kwargs.pop("from_zip", False)
    zip_cleanup = kwargs.pop("zip_cleanup", False)
    temp_dir = None

    input_path = kwargs.get("input_folder", "")

    if from_zip and input_path:
        if not os.path.isfile(input_path):
            print(f"ERROR: Archive not found: {input_path}", file=sys.stderr)
            return 1
        ext = os.path.splitext(input_path)[1].lower()
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        temp_dir = os.path.join(os.path.dirname(input_path) or ".", base_name)
        os.makedirs(temp_dir, exist_ok=True)
        print(f"Extracting {input_path} -> {temp_dir}")

        if ext in (".zip", ".cbz"):
            with zipfile.ZipFile(input_path, "r") as zf:
                zf.extractall(temp_dir)
        elif ext in (".rar", ".7z"):
            import subprocess
            exe = shutil.which("7z") or shutil.which("7z.exe") or ""
            if not exe:
                print("ERROR: 7-Zip not found. Install from https://7-zip.org to extract .rar/.7z files.", file=sys.stderr)
                shutil.rmtree(temp_dir, ignore_errors=True)
                return 1
            subprocess.run(
                [exe, "x", f"-o{temp_dir}", "-y", input_path],
                check=True,
            )
        else:
            print(f"ERROR: Unsupported archive format: {ext}", file=sys.stderr)
            shutil.rmtree(temp_dir, ignore_errors=True)
            return 1

        kwargs["input_folder"] = temp_dir

    try:
        process = ConsoleStitchProcess()
        result = process.run(kwargs)

        if zip_cleanup and result == 0:
            _do_zip_cleanup(kwargs.get("input_folder", ""), temp_dir)

        return result
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                print(f"Cleaned up: {temp_dir}")
            except Exception:
                pass


def _do_zip_cleanup(input_dir: str, temp_dir: str | None) -> None:
    """Zip processed output and delete intermediate folders."""
    import os
    import shutil
    import zipfile

    from core.utils.constants import OUTPUT_SUFFIX, POSTPROCESS_SUFFIX

    base = input_dir
    # If processing from zip, the original name is the zip name without ext
    stitched = base + OUTPUT_SUFFIX
    processed = base + POSTPROCESS_SUFFIX

    zip_src = processed if os.path.isdir(processed) else stitched
    if not os.path.isdir(zip_src):
        return

    zip_name = os.path.basename(os.path.normpath(base))
    zip_dest = os.path.join(os.path.dirname(base) or ".", f"{zip_name} [processed].zip")
    print(f"Creating {zip_dest}...")
    with zipfile.ZipFile(zip_dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(zip_src):
            for f in files:
                fpath = os.path.join(root, f)
                arcname = os.path.relpath(fpath, zip_src)
                zf.write(fpath, arcname)
    print(f"Created: {zip_dest}")

    for d in (stitched, processed):
        if os.path.isdir(d):
            try:
                shutil.rmtree(d)
                print(f"Removed: {d}")
            except Exception:
                pass
