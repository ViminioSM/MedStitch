import multiprocessing
import argparse
import winreg

from core.services import SettingsHandler
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

if __name__ == '__main__':
    # Required for multiprocessing on Windows
    multiprocessing.freeze_support()

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--preset",
        choices=["type", "redraw"],
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
    args = parser.parse_args()

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

    launch(
        preset=args.preset,
        input_path=args.input_path,
        waifu=args.waifu,
        watermark=(True if args.watermark == "on" else False if args.watermark == "off" else None),
        autostart=args.autostart,
    )
