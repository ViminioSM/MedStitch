"""Console stitch process — full feature parity with GUI via shared pipeline."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

from core.services.stitch_process import SettingsSnapshot, StitchProcess
from core.services.waifu2x_installer import (
    WAIFU_ARGS_JPG,
    WAIFU_ARGS_WEBP,
    WAIFU_EXE_PATH,
    ensure_waifu2x_installed,
    is_waifu2x_installed,
)
from core.utils.constants import (
    DETECTION_TYPE,
    WATERMARK_FULLPAGE_BLOCK_STRATEGY,
    WATERMARK_FULLPAGE_FREQUENCY,
    WATERMARK_FULLPAGE_POSITION,
    WATERMARK_OVERLAY_POSITION,
    WIDTH_ENFORCEMENT,
)

StatusFunc = Callable[[int | float, str], None]
ConsoleFunc = Callable[[str], None]

# Keys that map 1:1 onto SettingsSnapshot fields
_SNAPSHOT_KEYS = SettingsSnapshot.field_names()


def _status_printer(pct: int | float, message: str) -> None:
    print(f"[{int(pct):3d}%] {message}", flush=True)


def _console_printer(message: str) -> None:
    # Avoid double newlines when callers already append \n
    sys.stdout.write(message if message.endswith("\n") else message + "\n")
    sys.stdout.flush()


def _normalize_output_type(value: Any) -> str | None:
    """Normalize --output-type, including dual aliases (.webp+.png)."""
    if value is None:
        return None
    key = str(value).strip().lower()
    if key in {"dual", "webp+png", ".webp+.png", "webp_png"}:
        return ".webp+.png"
    if key and not key.startswith("."):
        key = f".{key}"
    return key


def _normalize_extra_outputs(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = str(value).replace(";", ",").split(",")
    out: list[str] = []
    for item in items:
        ext = str(item or "").strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        if ext not in out:
            out.append(ext)
    return out or None


def _detection_type_to_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    key = str(value).strip().lower()
    if key in {"none", "no", "0", "direct"}:
        return int(DETECTION_TYPE.NO_DETECTION)
    if key in {"pixel", "1", "smart"}:
        return int(DETECTION_TYPE.PIXEL_COMPARISON)
    raise ValueError(f"Invalid detection type: {value}")


def _enforce_type_to_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    key = str(value).strip().lower()
    mapping = {
        "none": int(WIDTH_ENFORCEMENT.NONE),
        "0": int(WIDTH_ENFORCEMENT.NONE),
        "auto": int(WIDTH_ENFORCEMENT.AUTOMATIC),
        "automatic": int(WIDTH_ENFORCEMENT.AUTOMATIC),
        "1": int(WIDTH_ENFORCEMENT.AUTOMATIC),
        "manual": int(WIDTH_ENFORCEMENT.MANUAL),
        "2": int(WIDTH_ENFORCEMENT.MANUAL),
    }
    if key not in mapping:
        raise ValueError(f"Invalid enforce type: {value}")
    return mapping[key]


def _enum_choice(value: str | int | None, table: dict[str, int]) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    key = str(value).strip().lower()
    if key.isdigit():
        return int(key)
    if key not in table:
        raise ValueError(f"Invalid choice '{value}'. Options: {', '.join(table)}")
    return table[key]


_FULLPAGE_POS = {
    "top": int(WATERMARK_FULLPAGE_POSITION.TOP),
    "center": int(WATERMARK_FULLPAGE_POSITION.CENTER),
    "bottom": int(WATERMARK_FULLPAGE_POSITION.BOTTOM),
}
_FULLPAGE_FREQ = {
    "once": int(WATERMARK_FULLPAGE_FREQUENCY.ONCE_PER_PAGE),
    "once_per_page": int(WATERMARK_FULLPAGE_FREQUENCY.ONCE_PER_PAGE),
    "all": int(WATERMARK_FULLPAGE_FREQUENCY.ALL_BLOCKS),
    "all_blocks": int(WATERMARK_FULLPAGE_FREQUENCY.ALL_BLOCKS),
    "alternating": int(WATERMARK_FULLPAGE_FREQUENCY.ALTERNATING),
}
_FULLPAGE_STRATEGY = {
    "first": int(WATERMARK_FULLPAGE_BLOCK_STRATEGY.FIRST),
    "best": int(WATERMARK_FULLPAGE_BLOCK_STRATEGY.BEST),
    "random": int(WATERMARK_FULLPAGE_BLOCK_STRATEGY.RANDOM),
}
_OVERLAY_POS = {
    "auto": int(WATERMARK_OVERLAY_POSITION.AUTO),
    "top_left": int(WATERMARK_OVERLAY_POSITION.TOP_LEFT),
    "top_right": int(WATERMARK_OVERLAY_POSITION.TOP_RIGHT),
    "bottom_left": int(WATERMARK_OVERLAY_POSITION.BOTTOM_LEFT),
    "bottom_right": int(WATERMARK_OVERLAY_POSITION.BOTTOM_RIGHT),
    "center": int(WATERMARK_OVERLAY_POSITION.CENTER),
}


def _join_paths(paths: list[str] | str | None) -> str | None:
    if paths is None:
        return None
    if isinstance(paths, str):
        return paths
    cleaned = [p.strip() for p in paths if p and str(p).strip()]
    return ";".join(cleaned) if cleaned else ""


def build_settings_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize CLI/JSON kwargs into SettingsSnapshot field overrides."""
    overrides: dict[str, Any] = {}

    # Direct pass-through of snapshot keys
    for key in _SNAPSHOT_KEYS:
        if key in raw and raw[key] is not None:
            overrides[key] = raw[key]

    # Dual output normalization (process once, save twice)
    if "output_type" in raw and raw["output_type"] is not None:
        normalized = _normalize_output_type(raw["output_type"])
        if normalized is not None:
            overrides["output_type"] = normalized
            if normalized == ".webp+.png":
                overrides["extra_output_types"] = [".png"]
    if "extra_output_types" in raw and raw["extra_output_types"] is not None:
        normalized_extra = _normalize_extra_outputs(raw["extra_output_types"])
        if normalized_extra is not None:
            overrides["extra_output_types"] = normalized_extra

    # Aliases used by historical console CLI
    if "detection_type" in raw and raw["detection_type"] is not None:
        overrides["detector_type"] = _detection_type_to_int(raw["detection_type"])
    if "detection_sensitivity" in raw and raw["detection_sensitivity"] is not None:
        overrides["sensitivity"] = int(raw["detection_sensitivity"])
    if "scan_line_step" in raw and raw["scan_line_step"] is not None:
        overrides["scan_step"] = int(raw["scan_line_step"])
    if "custom_width" in raw and raw["custom_width"] is not None:
        width = int(raw["custom_width"])
        if width > 0:
            overrides["enforce_type"] = int(WIDTH_ENFORCEMENT.MANUAL)
            overrides["enforce_width"] = width
        elif "enforce_type" not in overrides:
            overrides["enforce_type"] = int(WIDTH_ENFORCEMENT.NONE)

    if "enforce_type" in raw and raw["enforce_type"] is not None:
        overrides["enforce_type"] = _enforce_type_to_int(raw["enforce_type"])

    # Watermark path lists may arrive as list[str]
    for path_key in (
        "watermark_fullpage_paths",
        "watermark_overlay_paths",
        "watermark_header_paths",
        "watermark_footer_paths",
    ):
        if path_key in raw and raw[path_key] is not None:
            overrides[path_key] = _join_paths(raw[path_key])

    # Enum string aliases
    if raw.get("watermark_fullpage_position") is not None:
        overrides["watermark_fullpage_position"] = _enum_choice(
            raw["watermark_fullpage_position"], _FULLPAGE_POS
        )
    if raw.get("watermark_fullpage_frequency") is not None:
        overrides["watermark_fullpage_frequency"] = _enum_choice(
            raw["watermark_fullpage_frequency"], _FULLPAGE_FREQ
        )
    if raw.get("watermark_fullpage_block_strategy") is not None:
        overrides["watermark_fullpage_block_strategy"] = _enum_choice(
            raw["watermark_fullpage_block_strategy"], _FULLPAGE_STRATEGY
        )
    if raw.get("watermark_overlay_position") is not None:
        overrides["watermark_overlay_position"] = _enum_choice(
            raw["watermark_overlay_position"], _OVERLAY_POS
        )

    # Enable flags implied by path presence
    if overrides.get("watermark_fullpage_paths") and "watermark_fullpage_enabled" not in overrides:
        overrides["watermark_fullpage_enabled"] = True
    if overrides.get("watermark_overlay_paths") and "watermark_overlay_enabled" not in overrides:
        overrides["watermark_overlay_enabled"] = True
    if overrides.get("watermark_header_paths") and "watermark_header_enabled" not in overrides:
        overrides["watermark_header_enabled"] = True
    if overrides.get("watermark_footer_paths") and "watermark_footer_enabled" not in overrides:
        overrides["watermark_footer_enabled"] = True

    # Bool convenience: postprocess/comiczip/parallel/compact
    for bool_key in (
        "run_postprocess",
        "run_comiczip",
        "parallel_processing",
        "postprocess_compact_enabled",
        "watermark_fullpage_enabled",
        "watermark_overlay_enabled",
        "watermark_header_enabled",
        "watermark_footer_enabled",
        "watermark_fullpage_insert_mode",
        "watermark_fullpage_require_centered_space",
    ):
        if bool_key in raw and raw[bool_key] is not None:
            overrides[bool_key] = bool(raw[bool_key])

    # Drop Nones
    return {k: v for k, v in overrides.items() if v is not None and k in _SNAPSHOT_KEYS}


def load_json_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("JSON config root must be an object/dict.")
    return data


def _normalize_waifu_format(value: Any) -> str | None:
    """Return 'auto'|'jpg'|'webp', or None if waifu is disabled."""
    if value is None or value is False:
        return None
    if value is True:
        return "auto"
    key = str(value).strip().lower()
    if key in {"", "0", "false", "no", "off", "none"}:
        return None
    if key in {"1", "true", "yes", "on", "auto"}:
        return "auto"
    if key in {"jpg", "jpeg"}:
        return "jpg"
    if key == "webp":
        return "webp"
    raise ValueError(
        f"Invalid waifu format '{value}'. Use auto, jpg, webp, or true/false."
    )


def apply_waifu_preset(
    raw: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Enable Waifu2X postprocess with GUI-compatible defaults.

    Explicit --postprocess-app / --postprocess-args / --no-postprocess win.
    """
    fmt = _normalize_waifu_format(raw.get("waifu"))
    if fmt is None:
        return overrides

    # Honour explicit disable
    if raw.get("run_postprocess") is False:
        return overrides

    output_type = str(
        overrides.get("output_type") or raw.get("output_type") or ".png"
    ).lower()
    if fmt == "auto":
        fmt = "webp" if output_type.split("+")[0].strip().startswith(".webp") else "jpg"

    overrides["run_postprocess"] = True
    overrides.setdefault("postprocess_app", WAIFU_EXE_PATH)
    # Only fill args when missing/empty so CLI/JSON can override fully.
    existing_args = overrides.get("postprocess_args")
    if not existing_args:
        overrides["postprocess_args"] = (
            WAIFU_ARGS_WEBP if fmt == "webp" else WAIFU_ARGS_JPG
        )
    return overrides


def _should_auto_install_waifu(raw: dict[str, Any], overrides: dict[str, Any]) -> bool:
    """True when this run uses the default Waifu2X app path (auto-download allowed)."""
    if _normalize_waifu_format(raw.get("waifu")) is None:
        return False
    if raw.get("run_postprocess") is False:
        return False
    if not overrides.get("run_postprocess"):
        return False

    app = str(overrides.get("postprocess_app") or WAIFU_EXE_PATH).strip()
    if not app:
        return True

    # Custom bare command name in PATH — caller's responsibility.
    looks_like_path = os.path.isabs(app) or "/" in app or "\\" in app
    if not looks_like_path:
        return app in {WAIFU_EXE_PATH, os.path.basename(WAIFU_EXE_PATH)}

    # Only auto-install for the default install location.
    return os.path.abspath(app) == os.path.abspath(WAIFU_EXE_PATH)


def ensure_waifu_for_console(
    raw: dict[str, Any],
    overrides: dict[str, Any],
    *,
    console_func: ConsoleFunc,
    quiet: bool = False,
) -> dict[str, Any]:
    """If --waifu needs the default exe and it's missing, download like the GUI button."""
    repair = bool(raw.get("waifu_repair", False))
    waifu_requested = _normalize_waifu_format(raw.get("waifu")) is not None

    if not waifu_requested and not repair:
        return overrides
    if not _should_auto_install_waifu(raw, overrides) and not repair:
        return overrides

    app = str(overrides.get("postprocess_app") or WAIFU_EXE_PATH)
    if not repair and is_waifu2x_installed(app):
        overrides["postprocess_app"] = os.path.abspath(app)
        return overrides

    def _progress(received: int, total: int) -> None:
        if quiet:
            return
        if total > 0:
            pct = int(received * 100 / total)
            mb_r = received / (1024 * 1024)
            mb_t = total / (1024 * 1024)
            print(
                f"\rDownloading Waifu2X... {pct}% ({mb_r:.1f}/{mb_t:.1f} MB)",
                end="",
                flush=True,
            )
        else:
            mb_r = received / (1024 * 1024)
            print(f"\rDownloading Waifu2X... {mb_r:.1f} MB", end="", flush=True)

    installed = ensure_waifu2x_installed(
        exe_path=WAIFU_EXE_PATH,
        repair=repair,
        progress_callback=_progress if not quiet else None,
        console_func=console_func if not quiet else (lambda _m: None),
    )
    if not quiet:
        print(flush=True)  # newline after \r progress
    overrides["postprocess_app"] = installed
    overrides["run_postprocess"] = True
    return overrides


class ConsoleStitchProcess:
    """CLI entry that runs the same StitchProcess pipeline as the GUI."""

    def run(self, kwargs: dict[str, Any]) -> int:
        """Run stitch. Returns process exit code (0 success, 1 failure)."""
        raw = dict(kwargs)

        # Merge JSON config (CLI args already layered on top by launcher)
        config_path = raw.pop("config", None) or raw.pop("json_config", None)
        if config_path:
            file_cfg = load_json_config(str(config_path))
            # File is base; explicit kwargs override (except unset None)
            merged = dict(file_cfg)
            for key, value in raw.items():
                if value is not None:
                    merged[key] = value
            raw = merged

        input_folder = (
            raw.get("input_folder")
            or raw.get("input_path")
            or raw.get("input")
            or ""
        )
        input_folder = str(input_folder).strip()
        if not input_folder:
            raise ValueError("Missing input folder (-i / --input / config input_folder).")
        if not os.path.isdir(input_folder):
            raise FileNotFoundError(f"Input folder not found: {input_folder}")

        output_path = str(raw.get("output_path") or raw.get("output") or "").strip()
        postprocess_path = str(
            raw.get("postprocess_path") or raw.get("processed_path") or ""
        ).strip()

        use_saved_settings = bool(raw.get("use_saved_settings", False))
        psd_first_layer_only = bool(raw.get("psd_first_layer_only", False))
        quiet = bool(raw.get("quiet", False))

        overrides = build_settings_overrides(raw)

        # Legacy required split_height: if neither CLI/config/saved, keep AppSettings default
        if "split_height" not in overrides and raw.get("split_height") is not None:
            overrides["split_height"] = int(raw["split_height"])

        # Console-friendly defaults when not basing on the GUI profile.
        # Avoid silent downscale (GUI default is enforce=manual/800).
        if not use_saved_settings:
            console_defaults = {
                "output_type": ".png",
                "sensitivity": 90,
                "ignorable_pixels": 5,
                "scan_step": 30,
                "enforce_type": int(WIDTH_ENFORCEMENT.NONE),
                "parallel_processing": True,
                "lossy_quality": 100,
                "detector_type": int(DETECTION_TYPE.PIXEL_COMPARISON),
                "split_height": 5000,
            }
            for key, value in console_defaults.items():
                overrides.setdefault(key, value)

        # Waifu2X convenience preset (after defaults so auto can see output_type)
        overrides = apply_waifu_preset(raw, overrides)

        status_func: StatusFunc = (lambda _p, _m: None) if quiet else _status_printer
        console_func: ConsoleFunc = (lambda _m: None) if quiet else _console_printer

        if not quiet:
            print("--- SmartStitch Console ---", flush=True)
            print(f"Input : {os.path.abspath(input_folder)}", flush=True)
            if output_path:
                print(f"Output: {os.path.abspath(output_path)}", flush=True)
            print(
                f"Settings base: {'saved GUI profile' if use_saved_settings else 'defaults + CLI/JSON'}",
                flush=True,
            )

        # Auto-download Waifu2X when --waifu is used and the default exe is missing
        overrides = ensure_waifu_for_console(
            raw, overrides, console_func=console_func, quiet=quiet
        )

        if not quiet and overrides.get("run_postprocess"):
            print(
                f"Postprocess: {overrides.get('postprocess_app', '')} "
                f"{overrides.get('postprocess_args', '')}".rstrip(),
                flush=True,
            )

        process = StitchProcess()
        process.run(
            input_path=os.path.abspath(input_folder),
            output_path=os.path.abspath(output_path) if output_path else "",
            postprocess_path=os.path.abspath(postprocess_path) if postprocess_path else "",
            psd_first_layer_only=psd_first_layer_only,
            use_saved_settings=use_saved_settings,
            settings_overrides=overrides,
            mode="console",
            status_func=status_func,
            console_func=console_func,
        )
        return 0
