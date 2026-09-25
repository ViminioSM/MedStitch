import argparse
import multiprocessing
import os
import shutil
import subprocess
import sys
import zipfile

from core.services import SettingsHandler
from core.utils.constants import OUTPUT_SUFFIX, POSTPROCESS_SUFFIX
from gui.process import GuiStitchProcess

# Archives extractable via Python's zipfile module (bundled, fast)
_ZIP_EXTS = {".zip", ".cbz"}
# Archives requiring external 7z.exe
_SOLID_EXTS = {".rar", ".7z"}

_7Z_PATHS = (
    os.path.join(os.getenv("ProgramFiles", "C:\\Program Files"), "7-Zip", "7z.exe"),
    os.path.join(os.getenv("ProgramFiles(x86)", "C:\\Program Files (x86)"), "7-Zip", "7z.exe"),
    shutil.which("7z") or shutil.which("7z.exe") or "",
)


def _find_extractor() -> str | None:
    for path in _7Z_PATHS:
        if path and os.path.isfile(path):
            return path
    return None


def _extract_archive(archive_path: str, dest_dir: str) -> None:
    ext = os.path.splitext(archive_path)[1].lower()
    if ext in _ZIP_EXTS:
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest_dir)
        return
    if ext in _SOLID_EXTS:
        exe = _find_extractor()
        if exe is None:
            raise RuntimeError(
                "7-Zip not found. Install from https://7-zip.org to extract .rar/.7z files."
            )
        subprocess.run(
            [exe, "x", f"-o{dest_dir}", "-y", archive_path],
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return
    raise ValueError(f"Unsupported archive format: {ext}")


def _read_registry_bool(name: str) -> bool:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\SmartStitch\Shell", 0, winreg.KEY_READ
        ) as k:
            val, _ = winreg.QueryValueEx(k, name)
            return bool(val)
    except Exception:
        return False


def _custom_format_to_output_type(idx: int) -> str:
    mapping = {0: ".jpg", 1: ".png", 2: ".webp", 3: ".avif"}
    return mapping.get(int(idx), ".jpg")


def _output_type_to_custom_format(output_type: str) -> int:
    mapping = {".jpg": 0, ".jpeg": 0, ".png": 1, ".webp": 2, ".avif": 3}
    return mapping.get((output_type or "").lower(), 0)


def _apply_preset(settings: SettingsHandler, preset: str) -> None:
    """Apply an editable preset (uses the GUI Presets-tab values when stored)."""
    from core.services.settings_handler import apply_preset_to_settings

    preset = (preset or "").strip().lower()
    valid = {"type", "redraw", "custom", "personalizado", "personalizada",
             "dual", "duplo", "dupla"}
    if preset not in valid:
        raise ValueError("preset must be 'type', 'redraw', 'personalizado' or 'dual'")

    if preset in {"custom", "personalizado", "personalizada"}:
        name = "custom"
    elif preset in {"dual", "duplo", "dupla"}:
        name = "dual"
    else:
        name = preset
    apply_preset_to_settings(settings, name)


def launch() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--preset",
        required=True,
        choices=["type", "redraw", "custom", "personalizado", "personalizada",
                 "dual", "duplo", "dupla"],
        help="Which preset to use",
    )
    parser.add_argument(
        "--input",
        required=True,
        dest="input_path",
        help="Folder or archive path to process",
    )
    parser.add_argument(
        "--from-zip",
        action="store_true",
        default=False,
        dest="from_zip",
        help="Input is an archive (.zip .cbz .rar .7z) — extract first",
    )
    parser.add_argument(
        "--waifu",
        action="store_true",
        default=False,
        dest="waifu",
        help="Run Waifu2X after processing",
    )
    parser.add_argument(
        "--zip-cleanup",
        "--cleanup",
        action="store_true",
        default=False,
        dest="cleanup",
        help="Delete [stitched], zip [processed], delete [processed]",
    )
    args = parser.parse_args()

    # Read toggle state from Registry if not explicitly set
    if not args.cleanup and _read_registry_bool("ZipCleanup"):
        args.cleanup = True

    input_path = os.path.abspath(args.input_path)
    temp_dir = None

    if args.from_zip:
        if not os.path.isfile(input_path):
            raise FileNotFoundError(f"Archive file not found: {input_path}")
        ext = os.path.splitext(input_path)[1].lower()
        if ext not in (_ZIP_EXTS | _SOLID_EXTS):
            raise ValueError(f"Expected archive file: {input_path}")

        base_name = os.path.splitext(os.path.basename(input_path))[0]
        temp_dir = os.path.join(os.path.dirname(input_path), base_name)
        os.makedirs(temp_dir, exist_ok=True)

        print(f"Extracting {input_path} -> {temp_dir}...")
        _extract_archive(input_path, temp_dir)
        input_path = temp_dir
    else:
        if not os.path.isdir(input_path):
            raise FileNotFoundError(f"Input folder not found: {input_path}")

    settings = SettingsHandler()
    _apply_preset(settings, args.preset)

    if args.waifu:
        settings.save("run_postprocess", True)

    output_path = input_path + OUTPUT_SUFFIX

    try:
        process = GuiStitchProcess()
        process.run_with_error_msgs(
            input_path=input_path,
            output_path=output_path,
            status_func=lambda pct, msg: print(f"[{pct}%] {msg}"),
            console_func=print,
        )

        if args.cleanup:
            processed_path = input_path + POSTPROCESS_SUFFIX
            zip_src = processed_path if os.path.isdir(processed_path) else output_path
            if os.path.isdir(zip_src):
                base_name = os.path.basename(os.path.normpath(input_path))
                zip_dest = os.path.join(
                    os.path.dirname(input_path) or ".", f"{base_name} [processed].zip"
                )
                print(f"Creating {zip_dest}...")
                with zipfile.ZipFile(zip_dest, "w", zipfile.ZIP_DEFLATED) as zf:
                    for root, _, files in os.walk(zip_src):
                        for f in files:
                            fpath = os.path.join(root, f)
                            arcname = os.path.relpath(fpath, zip_src)
                            zf.write(fpath, arcname)
                print(f"Created: {zip_dest}")

                for d in (output_path, processed_path):
                    if os.path.isdir(d):
                        try:
                            shutil.rmtree(d)
                            print(f"Removed: {d}")
                        except Exception:
                            pass

        return 0
    finally:
        if temp_dir is not None and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                print(f"Cleaned up: {temp_dir}")
            except Exception:
                pass


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(launch())
