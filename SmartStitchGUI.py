"""SmartStitch GUI entry point."""

import os
import sys
import traceback

# --- Early crash log: log startup errors to file WITHOUT redirecting stderr ---
def _startup_crash_log() -> str | None:
    """Return path to crash log file, or None if logging can't be set up."""
    try:
        appdata = os.getenv("APPDATA") or os.path.expanduser("~")
        log_dir = os.path.join(appdata, "SmartStitch", "__logs__")
        os.makedirs(log_dir, exist_ok=True)
        from datetime import datetime
        return os.path.join(
            log_dir,
            f"crash_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.log"
        )
    except Exception:
        return None

_CRASH_LOG = _startup_crash_log()

import multiprocessing
import argparse
import winreg

from core.services import SettingsHandler
from core.services.global_logger import GlobalLogger
from core.i18n import init as init_i18n, set_language, current_lang
from gui.launcher import launch


_WM_KEYS = (
    "watermark_fullpage_enabled",
    "watermark_overlay_enabled",
    "watermark_header_enabled",
    "watermark_footer_enabled",
)
_WM_RESTORE_FLAG = "watermark_restore_saved"
_WM_RESTORE_PREFIX = "watermark_restore_"
_REG_BASE_KEYS = (
    r"Software\Classes\Directory\shell\MedStitch",
    r"Software\Classes\Directory\Background\shell\MedStitch",
)


def _load_bool(settings: SettingsHandler, key: str, default: bool = False) -> bool:
    try:
        return bool(settings.load(key))
    except Exception:
        return default


def _set_watermark_state(settings: SettingsHandler, enabled: bool) -> None:
    for key in _WM_KEYS:
        settings.save(key, enabled)


def _snapshot_current_watermark_state(settings: SettingsHandler) -> None:
    for key in _WM_KEYS:
        settings.save(f"{_WM_RESTORE_PREFIX}{key}", _load_bool(settings, key, False))
    settings.save(_WM_RESTORE_FLAG, True)


def _restore_previous_watermark_state(settings: SettingsHandler) -> bool:
    if not _load_bool(settings, _WM_RESTORE_FLAG, False):
        return False

    for key in _WM_KEYS:
        restore_key = f"{_WM_RESTORE_PREFIX}{key}"
        settings.save(key, _load_bool(settings, restore_key, False))
    return True


def _has_any_watermark_enabled(settings: SettingsHandler) -> bool:
    return any(_load_bool(settings, key, False) for key in _WM_KEYS)


def _refresh_context_menu_watermark_label(currently_enabled: bool) -> None:
    action_label = "Desativar Marcas d'agua" if currently_enabled else "Ativar Marcas d'agua"
    for base in _REG_BASE_KEYS:
        key_path = base + r"\shell\WatermarkToggle"
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                key_path,
                0,
                winreg.KEY_SET_VALUE,
            ) as k:
                winreg.SetValueEx(k, None, 0, winreg.REG_SZ, action_label)
        except FileNotFoundError:
            pass

def _handle_toggle(toggle_zip: bool) -> None:
    """Flip toggle state in Registry and reinstall context menu."""
    if toggle_zip:
        current = _read_toggle("ZipCleanup")
        _write_toggle("ZipCleanup", not current)

    # Reinstall context menu to update labels
    from gui.controller import _install_context_menu as install_cm
    try:
        install_cm()
    except Exception:
        pass  # QMessageBox fails without QApplication, but registry is updated


def _read_toggle(name: str) -> bool:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\SmartStitch\Shell", 0, winreg.KEY_READ
        ) as k:
            val, _ = winreg.QueryValueEx(k, name)
            return bool(val)
    except FileNotFoundError:
        return False


def _write_toggle(name: str, value: bool) -> None:
    try:
        key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, r"Software\SmartStitch\Shell"
        )
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
        winreg.CloseKey(key)
    except Exception:
        pass


def _main():
    """Main entry point (wrapped for logging)."""
    # Direct crash log write (bypasses logging — catches early failures)
    if _CRASH_LOG:
        try:
            with open(_CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(f"TRACE _main() entered: argv={sys.argv}\n")
                f.flush()
        except Exception:
            pass

    # Initialize i18n on first run, then load saved preference
    saved_lang = SettingsHandler().load("app_language") or ""
    lang = init_i18n(saved_lang or None)
    if lang != saved_lang:
        SettingsHandler().save("app_language", lang)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--preset",
        choices=["type", "redraw", "custom", "personalizado", "personalizada",
                 "dual", "duplo", "dupla"],
        default=None,
    )
    parser.add_argument(
        "--input",
        dest="input_path",
        default=None,
    )
    parser.add_argument(
        "--waifu",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--watermark",
        choices=["on", "off"],
        default=None,
    )
    parser.add_argument(
        "--set-watermark",
        choices=["on", "off"],
        default=None,
        dest="set_watermark",
    )
    parser.add_argument(
        "--toggle-watermark",
        action="store_true",
        default=False,
        dest="toggle_watermark",
    )
    parser.add_argument(
        "--autostart",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--from-zip",
        action="store_true",
        default=False,
        dest="from_zip",
    )
    parser.add_argument(
        "--toggle-waifu",
        action="store_true",
        default=False,
        dest="toggle_waifu_deprecated",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--toggle-zip",
        "--toggle-compact",
        "--toggle-cleanup",
        "--compact",
        action="store_true",
        default=False,
        dest="toggle_zip",
    )
    parser.add_argument(
        "--zip-cleanup",
        "--cleanup",
        "--compactar",
        action="store_true",
        default=False,
        dest="zip_cleanup",
        help="Delete [stitched], zip [processed], delete [processed] after processing",
    )
    args = parser.parse_args()

    # Handle toggle commands (Registry state flip + context menu reinstall)
    if args.toggle_zip:
        _handle_toggle(args.toggle_zip)
        raise SystemExit(0)

    # Handle archive extraction before launching GUI
    input_path = args.input_path
    if args.from_zip and input_path:
        if not os.path.isfile(input_path):
            print(f"Archive not found: {input_path}", file=sys.stderr)
            raise SystemExit(1)
        ext = os.path.splitext(input_path)[1].lower()
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        temp_dir = os.path.join(os.path.dirname(input_path), base_name)
        os.makedirs(temp_dir, exist_ok=True)
        print(f"Extracting {input_path} -> {temp_dir}")

        if ext in (".zip", ".cbz"):
            import zipfile
            with zipfile.ZipFile(input_path, "r") as zf:
                zf.extractall(temp_dir)
        elif ext in (".rar", ".7z"):
            import shutil
            import subprocess
            exe = shutil.which("7z") or shutil.which("7z.exe") or ""
            if not exe:
                print("7-Zip not found. Install from https://7-zip.org", file=sys.stderr)
                raise SystemExit(1)
            subprocess.run(
                [exe, "x", f"-o{temp_dir}", "-y", input_path],
                check=True,
            )
        else:
            print(f"Unsupported archive format: {ext}", file=sys.stderr)
            raise SystemExit(1)

        input_path = temp_dir

    if args.set_watermark is not None:
        enabled = args.set_watermark == "on"
        settings = SettingsHandler()
        currently_enabled = _has_any_watermark_enabled(settings)

        if enabled:
            if not currently_enabled:
                restored = _restore_previous_watermark_state(settings)
                if not restored:
                    _set_watermark_state(settings, True)
        else:
            if currently_enabled:
                _snapshot_current_watermark_state(settings)
                _set_watermark_state(settings, False)

        _refresh_context_menu_watermark_label(_has_any_watermark_enabled(settings))
        raise SystemExit(0)

    if args.toggle_watermark:
        settings = SettingsHandler()
        if _has_any_watermark_enabled(settings):
            _snapshot_current_watermark_state(settings)
            _set_watermark_state(settings, False)
        else:
            restored = _restore_previous_watermark_state(settings)
            if not restored:
                _set_watermark_state(settings, True)
        _refresh_context_menu_watermark_label(_has_any_watermark_enabled(settings))
        raise SystemExit(0)

    if _CRASH_LOG:
        try:
            with open(_CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(f"TRACE before launch: input={input_path} preset={args.preset} waifu={args.waifu} autostart={args.autostart}\n")
                f.flush()
        except Exception:
            pass

    launch(
        preset=args.preset,
        input_path=input_path,
        waifu=args.waifu,
        watermark=(True if args.watermark == "on" else False if args.watermark == "off" else None),
        autostart=args.autostart,
    )

    # Run cleanup after the GUI closes (autostart auto-closes when done)
    if args.zip_cleanup and input_path:
        _run_zip_cleanup(input_path, remove_input_dir=args.from_zip)


def _run_zip_cleanup(input_dir: str, remove_input_dir: bool = False) -> None:
    """Delete [stitched], zip [processed] as .zip, delete [processed]."""
    import shutil
    import zipfile

    stitched = input_dir + " [stitched]"
    processed = input_dir + " [processed]"

    zip_src = processed if os.path.isdir(processed) else stitched
    if not os.path.isdir(zip_src):
        return

    # Name the output zip so it never collides with an input .zip
    base_name = os.path.basename(os.path.normpath(input_dir))
    zip_dest = os.path.join(os.path.dirname(input_dir) or ".", f"{base_name} [processed].zip")
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

    # When input came from an extracted archive, remove the temp folder too
    if remove_input_dir and os.path.isdir(input_dir):
        try:
            shutil.rmtree(input_dir)
            print(f"Removed extracted folder: {input_dir}")
        except Exception:
            pass

if __name__ == '__main__':
    multiprocessing.freeze_support()
    try:
        GlobalLogger.configure()
        GlobalLogger.install_excepthook()
        GlobalLogger.log_startup()
        _main()
    except Exception:
        if _CRASH_LOG:
            with open(_CRASH_LOG, "a", encoding="utf-8") as f:
                traceback.print_exc(file=f)
        raise
    finally:
        try:
            GlobalLogger.log_shutdown()
        except Exception:
            pass