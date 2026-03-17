import json
import os
from typing import Any

from core.utils.errors import ProfileException

from ..models import AppProfiles, AppSettings
from ..services import logFunc
from ..utils.constants import SETTINGS_REL_DIR


_SETTINGS_MIGRATIONS = [
    # (old_key, new_key) - migrates old settings to new keys
    ("senstivity", "sensitivity"),
]


def _migrate_profile(profile: dict) -> dict:
    """Apply migrations to a profile dict for backwards compatibility."""
    for old_key, new_key in _SETTINGS_MIGRATIONS:
        if old_key in profile and new_key not in profile:
            profile[new_key] = profile.pop(old_key)
        elif old_key in profile:
            del profile[old_key]
    return profile


class SettingsHandler:
    def __init__(self):
        self.settings_file = os.path.join(SETTINGS_REL_DIR, 'settings.json')
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
        return AppSettings(
            self.current_profiles.profiles[self.current_profiles.current]
        )

    def save_current_settings(self, settings: AppSettings | None = None) -> AppSettings:
        """Saves application settings to current profile"""
        if not (settings):
            settings = AppSettings()
        current_profile = self.current_profiles.profiles[self.current_profiles.current]
        self.current_profiles.profiles[self.current_profiles.current] = {
            **current_profile,
            **vars(settings),
        }
        self.save_all(self.current_profiles)
        return settings

    # Profile Handling Logic
    def get_current_index(self) -> int:
        return self.current_profiles.current

    @logFunc(inclass=True)
    def set_current_index(self, new_index: int):
        self.current_profiles.current = new_index
        self.save_all(self.current_profiles)
        self.current_settings = self.load_current_settings()

    def get_current_profile_name(self) -> str:
        index = self.current_profiles.current
        if index >= len(self.current_profiles.profiles):
            raise ProfileException('Current profile not found')
        return str(self.current_profiles.profiles[index].get("profile_name", ""))

    def set_current_profile_name(self, new_name):
        index = self.current_profiles.current
        if index >= len(self.current_profiles.profiles):
            raise ProfileException('Current profile not found')
        self.current_profiles.profiles[index]["profile_name"] = new_name
        self.save_all(self.current_profiles)

    def get_profile_names(self) -> list[str]:
        names = []
        for profile in self.current_profiles.profiles:
            names.append(profile.get("profile_name"))
        return names

    @logFunc(inclass=True)
    def add_profile(self, profile_name: str | None = None):
        """Adds a settings profile to the collection"""
        if not profile_name:
            profile_name = "Settings Profile " + str(
                len(self.current_profiles.profiles) + 1
            )
        self.current_profiles.profiles.append(
            {"profile_name": profile_name, **vars(self.current_settings)}
        )
        self.save_all(self.current_profiles)
        return profile_name

    @logFunc(inclass=True)
    def remove_profile(self, index: int):
        if len(self.current_profiles.profiles) == 1:
            raise ProfileException('Last existing profile can not be removed')
        del self.current_profiles.profiles[index]
        self.set_current_index(0)
        self.save_all(self.current_profiles)

    def load_all(self) -> AppProfiles:
        """Loads settings profile pointers from a json file."""
        if not os.path.exists(self.settings_file):
            return self.save_all()
        else:
            with open(self.settings_file, "r") as f:
                return AppProfiles(json.load(f))

    def save_all(self, profiles: AppProfiles | None = None) -> AppProfiles:
        """Saves settings profile pointers from a json file."""
        if not os.path.exists(SETTINGS_REL_DIR):
            os.makedirs(SETTINGS_REL_DIR)
        if not (profiles):
            profiles = AppProfiles()
        with open(self.settings_file, "w") as f:
            json.dump(vars(profiles), f, indent=2)
        return profiles
