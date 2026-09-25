import json
import os
from typing import Any

from ..models import AppProfiles, AppSettings
from ..services import logFunc
from ..utils.constants import SETTINGS_REL_DIR

_SETTINGS_MIGRATIONS = [
    # (old_key, new_key) - migrates old settings to new keys
    ("senstivity", "sensitivity"),
]

_LEGACY_WAIFU_JPG_ARGS = "-i [stitched] -o [processed] -n 3 -s 1 -f jpg"
_LEGACY_WAIFU_JPG_ARGS_Q = "-i [stitched] -o [processed] -n 3 -s 1 -f jpg -q 100"
_UPDATED_WAIFU_JPG_ARGS = "-i [stitched] -o [processed] -n 3 -s 1 -f jpg"


_PRESET_DEFAULTS = {
    "type": {
        "output_type": ".webp",
        "enforce_width": 800,
        "detector_type": 0,
        "split_height": 15000,
        "lossy_quality": 100,
        "postprocess_args": "-i [stitched] -o [processed] -n 3 -s 1 -f webp",
    },
    "redraw": {
        "output_type": ".jpg",
        "enforce_width": 800,
        "detector_type": 1,
        "split_height": 15000,
        "lossy_quality": 100,
        "sensitivity": 100,
        "scan_step": 30,
        "ignorable_pixels": 0,
        "postprocess_args": "-i [stitched] -o [processed] -n 3 -s 1 -f jpg",
    },
    "custom": {
        "output_type": ".jpg",
        "enforce_width": 800,
        "detector_type": 1,
        "split_height": 15000,
        "lossy_quality": 100,
        "sensitivity": 100,
        "scan_step": 30,
        "ignorable_pixels": 0,
        "postprocess_args": "-i [stitched] -o [processed] -n 3 -s 1 -f jpg",
    },
    "dual": {
        "output_type": ".webp+.png",
        "extra_output_types": [".png"],
        "enforce_width": 800,
        "detector_type": 0,
        "split_height": 15000,
        "lossy_quality": 100,
        "postprocess_args": "-i [stitched] -o [processed] -n 3 -s 1 -f webp",
    },
}


def get_preset(settings_handler: "SettingsHandler", name: str) -> dict:
    """Return the editable preset dict for *name* (defaults merged in).

    Stored under the "presets" key so the GUI Presets tab can edit every
    field (largura/detector/formato/corte) without hard-coded values.
    """
    name = str(name or "").strip().lower()
    defaults = dict(_PRESET_DEFAULTS.get(name, {}))
    try:
        current = settings_handler.load("presets") or {}
    except Exception:
        current = {}
    stored = dict(current.get(name, {}) or {}) if isinstance(current, dict) else {}
    merged = {**defaults, **stored}
    return merged


def save_preset(settings_handler: "SettingsHandler", name: str, values: dict) -> dict:
    """Persist editable preset fields and return the merged preset."""
    name = str(name or "").strip().lower()
    if name not in _PRESET_DEFAULTS:
        raise ValueError(f"Unknown preset: {name}")
    allowed = {
        "output_type", "extra_output_types", "enforce_width", "detector_type",
        "split_height", "lossy_quality", "sensitivity", "scan_step",
        "ignorable_pixels", "postprocess_args",
    }
    cleaned = {k: v for k, v in (values or {}).items() if k in allowed}
    try:
        current = settings_handler.load("presets") or {}
    except Exception:
        current = {}
    if not isinstance(current, dict):
        current = {}
    stored = dict(current.get(name, {}) or {})
    stored.update(cleaned)
    current[name] = stored
    settings_handler.save("presets", current)
    return {**_PRESET_DEFAULTS[name], **stored}


def reset_preset(settings_handler: "SettingsHandler", name: str) -> dict:
    name = str(name or "").strip().lower()
    try:
        current = settings_handler.load("presets") or {}
    except Exception:
        current = {}
    if isinstance(current, dict) and name in current:
        del current[name]
        settings_handler.save("presets", current)
    return dict(_PRESET_DEFAULTS.get(name, {}))


def apply_preset_to_settings(settings_handler: "SettingsHandler", name: str) -> dict:
    """Apply an editable preset to the live settings. Returns applied values."""
    preset = get_preset(settings_handler, name)
    applied = {
        "output_type": preset.get("output_type", ".jpg"),
        "extra_output_types": preset.get("extra_output_types", []),
        "lossy_quality": int(preset.get("lossy_quality", 100)),
        "split_height": int(preset.get("split_height", 15000)),
        "enforce_type": 2,
        "enforce_width": int(preset.get("enforce_width", 800)),
        "detector_type": int(preset.get("detector_type", 1)),
        "postprocess_args": preset.get("postprocess_args", ""),
    }
    if int(preset.get("detector_type", 1)) == 1:
        applied.update(
            {
                "sensitivity": int(preset.get("sensitivity", 100)),
                "scan_step": int(preset.get("scan_step", 30)),
                "ignorable_pixels": int(preset.get("ignorable_pixels", 0)),
            }
        )
    for key, value in applied.items():
        settings_handler.save(key, value)
    return applied


def _migrate_profile(profile: dict) -> dict:
    """Apply migrations to a profile dict for backwards compatibility."""
    for old_key, new_key in _SETTINGS_MIGRATIONS:
        if old_key in profile and new_key not in profile:
            profile[new_key] = profile.pop(old_key)
        elif old_key in profile:
            del profile[old_key]

    # Keep existing user behavior but raise default JPG output quality for Waifu2X.
    if profile.get("postprocess_args") == _LEGACY_WAIFU_JPG_ARGS:
        profile["postprocess_args"] = _UPDATED_WAIFU_JPG_ARGS
    # Strip obsolete -q 100 that waifu2x-ncnn-vulkan doesn't use.
    if profile.get("postprocess_args") == _LEGACY_WAIFU_JPG_ARGS_Q:
        profile["postprocess_args"] = _UPDATED_WAIFU_JPG_ARGS
    if profile.get("extra_output_types") is None:
        profile["extra_output_types"] = []
    if profile.get("presets") is None:
        profile["presets"] = {}
    return profile


class SettingsHandler:
    def __init__(self):
        self.settings_file = os.path.join(SETTINGS_REL_DIR, "settings.json")
        self.current_profiles = self.load_all()
        self._apply_migrations()
        self.current_settings = self.load_current_settings()

    def _apply_migrations(self) -> None:
        """Apply migrations to all profiles."""
        migrated = False
        for profile in self.current_profiles.profiles:
            old_keys = set(profile.keys())
            _migrate_profile(profile)
            if set(profile.keys()) != old_keys:
                migrated = True
        if migrated:
            self.save_all(self.current_profiles)

    def load(self, key: str) -> Any:
        """Loads the value of a single setting key"""
        return self.current_settings.__dict__[key]

    @logFunc(inclass=True)
    def save(self, key: str, value: Any):
        """Updates a single setting value"""
        self.current_settings.__dict__[key] = value
        self.save_current_settings(self.current_settings)

    def load_current_settings(self) -> AppSettings:
        """Loads application settings from current profile"""
        if not self.current_profiles.profiles:
            return AppSettings()
        current = min(
            max(0, self.current_profiles.current),
            len(self.current_profiles.profiles) - 1,
        )
        return AppSettings(self.current_profiles.profiles[current])

    def save_current_settings(self, settings: AppSettings | None = None) -> AppSettings:
        """Saves application settings to current profile"""
        if not settings:
            settings = AppSettings()
        if not self.current_profiles.profiles:
            self.current_profiles = AppProfiles()
        current = min(
            max(0, self.current_profiles.current),
            len(self.current_profiles.profiles) - 1,
        )
        self.current_profiles.current = current
        current_profile = self.current_profiles.profiles[current]
        self.current_profiles.profiles[current] = {
            **current_profile,
            **vars(settings),
        }
        self.save_all(self.current_profiles)
        return settings

    def load_all(self) -> AppProfiles:
        """Loads settings profile pointers from a json file."""
        if not os.path.exists(self.settings_file):
            return self.save_all()
        with open(self.settings_file, "r") as f:
            return AppProfiles(json.load(f))

    def save_all(self, profiles: AppProfiles | None = None) -> AppProfiles:
        """Saves settings profile pointers from a json file."""
        if not os.path.exists(SETTINGS_REL_DIR):
            os.makedirs(SETTINGS_REL_DIR)
        if not profiles:
            profiles = AppProfiles()
        with open(self.settings_file, "w") as f:
            json.dump(vars(profiles), f, indent=2)
        return profiles
