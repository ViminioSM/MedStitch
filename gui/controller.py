import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import urllib.request
import webbrowser
import winreg
import zipfile
from typing import Any, Callable
from urllib.parse import urlparse

import logging

from core.i18n import t as _t
from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, Signal

_log = logging.getLogger("smartstitch")
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMessageBox,
    QProgressDialog,
)

from assets.SmartStitchLogo import icon
from core.services import SettingsHandler
from core.services.settings_handler import (
    apply_preset_to_settings,
    get_preset,
    reset_preset,
    save_preset,
)
from core.services.waifu2x_installer import (
    WAIFU_ARGS_AVIF,
    WAIFU_ARGS_JPG,
    WAIFU_ARGS_PNG,
    WAIFU_ARGS_WEBP,
    WAIFU_EXE_PATH,
    WAIFU_INSTALL_DIR,
    WAIFU_ZIP_URL,
    download_and_extract_waifu2x,
)
from core.utils.constants import DUAL_OUTPUT_TYPE
from core.utils.constants import OUTPUT_SUFFIX
from gui.build_version import APP_BUILD_VERSION
from gui.process import GuiStitchProcess

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)


def _load_app_version() -> str:
    env_version = os.getenv(
        "MEDSTITCH_VERSION", os.getenv("SMARTSTITCH_VERSION", "")
    ).strip()
    if env_version:
        return env_version

    if APP_BUILD_VERSION and APP_BUILD_VERSION != "0.0.0":
        return APP_BUILD_VERSION

    try:
        commit_title = subprocess.check_output(  # nosec B603 B607
            ["git", "log", "-1", "--pretty=%s"],
            cwd=_PROJECT_ROOT,
            text=True,
            timeout=3,
        ).strip()
        match = re.search(r"\bv?(\d+\.\d+\.\d+)\b", commit_title)
        if match:
            return match.group(1)
    except Exception:  # nosec B110
        pass

    return APP_BUILD_VERSION or "0.0.0"


APP_NAME = "MedStitch"
APP_VENDOR = "ViminioSM"
APP_VERSION = _load_app_version()
GITHUB_REPO = "ViminioSM/MedStitch"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
GITHUB_API_LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
AUTO_CHECK_UPDATES_ON_STARTUP = False
AUTO_UPDATE_ON_STARTUP = False


_CONTEXT_MENU_GUI = os.path.join(_PROJECT_ROOT, "SmartStitchGUI.py")
_ICON_FILE = os.path.join(_PROJECT_ROOT, "assets", "SmartStitchLogo.ico")

_REG_BASE_KEYS = (
    r"Software\Classes\Directory\shell\MedStitch",
    r"Software\Classes\Directory\Background\shell\MedStitch",
    r"Software\Classes\SystemFileAssociations\.zip\shell\MedStitch",
    r"Software\Classes\SystemFileAssociations\.cbz\shell\MedStitch",
    r"Software\Classes\SystemFileAssociations\.rar\shell\MedStitch",
    r"Software\Classes\SystemFileAssociations\.7z\shell\MedStitch",
)

_LEGACY_REG_BASE_KEYS = (
    r"Software\Classes\Directory\shell\SmartStitch",
    r"Software\Classes\Directory\Background\shell\SmartStitch",
)

# COM Shell Extension handlers registered by the failed C++ DLL experiment.
# These must be removed or the old broken menu keeps appearing on zip files.
_COM_SHELLEX_KEYS = (
    r"Software\Classes\CLSID\{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}",
    r"Software\Classes\Directory\ShellEx\ContextMenuHandlers\MedStitch",
    r"Software\Classes\Directory\Background\ShellEx\ContextMenuHandlers\MedStitch",
    r"Software\Classes\SystemFileAssociations\.zip\ShellEx\ContextMenuHandlers\MedStitch",
    r"Software\Classes\SystemFileAssociations\.cbz\ShellEx\ContextMenuHandlers\MedStitch",
    r"Software\Classes\SystemFileAssociations\.rar\ShellEx\ContextMenuHandlers\MedStitch",
    r"Software\Classes\SystemFileAssociations\.7z\ShellEx\ContextMenuHandlers\MedStitch",
    r"Software\Classes\*\ShellEx\ContextMenuHandlers\MedStitch",
)

_REG_TOGGLE_KEY = r"Software\SmartStitch\Shell"

_CONTEXT_MENU_ENTRIES = (
    ("OpenApp", "Abrir SmartStitch", None, False, None, False, False, False, False),
    ("Redraw", "Redraw", "redraw", False, None, True, True, False, False),
    ("Type", "Type", "type", False, None, True, True, False, False),
    ("RedrawWaifu", "Redraw + Waifu", "redraw", True, None, True, True, False, False),
    ("TypeWaifu", "Type + Waifu", "type", True, None, True, True, False, False),
    ("ToggleCompact", "", None, False, None, False, False, False, False),
    ("WatermarkToggle", "Mudar Marcas d'agua", None, False, None, False, False, False, False),
)

_ZIP_CONTEXT_MENU_ENTRIES = (
    ("OpenApp", "Abrir SmartStitch", None, False, None, False, False, True, False),
    ("Redraw", "Redraw", "redraw", False, None, True, True, True, False),
    ("Type", "Type", "type", False, None, True, True, True, False),
    ("RedrawWaifu", "Redraw + Waifu", "redraw", True, None, True, True, True, False),
    ("TypeWaifu", "Type + Waifu", "type", True, None, True, True, True, False),
    ("ToggleCompact", "", None, False, None, False, False, True, False),
)

# ── Module-level state (set once by initialize_gui) ──────────────────────────
_main_window: Any = None
_settings: Any = None
_process_thread: "ProcessThread | None" = None
_folder_drop_filter: "FolderDropFilter | None" = None
_auto_close_on_finish: bool = False
_job_queue: list[dict] = []
_job_running: bool = False

_WATERMARK_KEYS = (
    "watermark_fullpage_enabled",
    "watermark_overlay_enabled",
    "watermark_header_enabled",
    "watermark_footer_enabled",
)


_SAFE_UPDATE_HOSTS = (
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "githubusercontent.com",
)


def _assert_safe_http_url(url: str, *, allowed_hosts: tuple[str, ...]) -> str:
    """Validate URL scheme and host before network calls."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme or '<empty>'}")
    if not host:
        raise ValueError("URL host is missing.")
    if not any(
        host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts
    ):
        raise ValueError(f"Host is not allowed for download: {host}")
    return parsed.geturl()


class FolderDropFilter(QObject):
    """Event filter that lets QLineEdit fields accept folder drag-and-drop.

    When the user drags a directory from the OS file explorer and drops it
    onto a registered line edit, the line edit text is replaced with the
    directory path. No visual hint is added; behavior-only.
    """

    def eventFilter(self, obj, event):  # type: ignore[override]
        etype = event.type()
        if etype not in (QEvent.Type.DragEnter, QEvent.Type.Drop):
            return super().eventFilter(obj, event)

        mime = event.mimeData()
        if not mime or not mime.hasUrls():
            return False

        for url in mime.urls():
            path = url.toLocalFile()
            if path and os.path.isdir(path):
                if etype == QEvent.Type.Drop:
                    obj.setText(path)
                event.acceptProposedAction()
                return True

        return False


class ProcessThread(QThread):
    progress = Signal(int, str)
    postProcessConsole = Signal(str)
    showWarning = Signal(str, str)
    showError = Signal(str, str)
    showInfo = Signal(str, str)

    def __init__(self, parent):
        super().__init__(parent)
        self._input_path = ""
        self._output_path = ""

    def configure(self, input_path: str, output_path: str) -> None:
        """Configure thread parameters before starting."""
        self._input_path = input_path
        self._output_path = output_path

    def run(self) -> None:
        if not self._input_path:
            _log.warning("ProcessThread.run: no input_path")
            return
        _log.info("ProcessThread started: input=%s", self._input_path)
        try:
            GuiStitchProcess().run_with_error_msgs(
                input_path=self._input_path,
                output_path=self._output_path,
                status_func=self.progress.emit,
                console_func=self.postProcessConsole.emit,
            )
            _log.info("ProcessThread completed successfully: input=%s", self._input_path)
        except Exception as e:
            _log.exception("ProcessThread failed: input=%s, error=%s", self._input_path, e)
            raise


def _apply_translations() -> None:
    """Update all UI strings based on current language. Covers every visible widget."""
    w = _main_window

    # ── Tab titles (looked up by page widget, not fixed index) ──
    _TAB_TEXT_KEYS = (
        ("mainTab", "tab.basic"),
        ("postProcessTab", "tab.postprocess"),
        ("presetsTab", "tab.presets"),
        ("watermarkTab", "tab.watermark"),
    )
    for _page_name, _key in _TAB_TEXT_KEYS:
        try:
            _page = getattr(w, _page_name, None)
            if _page is None:
                continue
            _idx = w.mainTabWidget.indexOf(_page)
            if _idx >= 0:
                w.mainTabWidget.setTabText(_idx, _t(_key))
        except (AttributeError, TypeError):
            pass

    # ── Group box titles (Basic tab) ──
    w.browseGroupBox.setTitle(_t("group.input_output"))
    w.generalGroupBox.setTitle(_t("group.general"))
    w.postProcessGroupBox.setTitle(_t("group.postprocess"))
    w.ActionGroupBox.setTitle(_t("group.status"))

    # ── Group box titles (Watermark tab) ──
    w.watermarkFullpageGroupBox.setTitle(_t("group.watermark_fullpage"))
    w.watermarkOverlayGroupBox.setTitle(_t("group.watermark_overlay"))
    w.watermarkHeaderFooterGroupBox.setTitle(_t("group.watermark_header_footer"))
    try:
        w.watermarkChapterSkipGroupBox.setTitle(_t("group.watermark_frequency"))
    except AttributeError:
        pass

    # ── Labels (Basic tab) ──
    try: w.inputLabel.setText(_t("label.input"))
    except AttributeError: pass
    try: w.heightLabel.setText(_t("label.split_height"))
    except AttributeError: pass
    try: w.customWidthLabel.setText(_t("label.custom_width"))
    except AttributeError: pass
    try: w.customDetectorLabel.setText(_t("label.custom_detector"))
    except AttributeError: pass
    try: w.customFormatLabel.setText(_t("label.custom_format"))
    except AttributeError: pass

    # ── Labels (Watermark tab - Fullpage) ──
    try: w.wmFullpageThresholdLabel.setText(_t("label.wm_threshold"))
    except AttributeError: pass
    try: w.wmFullpageMaxLabel.setText(_t("label.wm_max_per_page"))
    except AttributeError: pass
    try: w.wmFullpageMinAreaLabel.setText(_t("label.wm_min_area"))
    except AttributeError: pass
    try: w.wmFullpageSpacingTopLabel.setText(_t("label.wm_spacing_top"))
    except AttributeError: pass
    try: w.wmFullpageSpacingBottomLabel.setText(_t("label.wm_spacing_bottom"))
    except AttributeError: pass
    try: w.wmChapterSkipLabel.setText(_t("label.chapter_skip"))
    except AttributeError: pass

    # ── Labels (Watermark tab - Overlay) ──
    try: w.wmOverlayPositionLabel.setText(_t("label.wm_overlay_position"))
    except AttributeError: pass
    try: w.wmOverlayOpacityLabel.setText(_t("label.wm_overlay_opacity"))
    except AttributeError: pass
    try: w.wmOverlayScaleLabel.setText(_t("label.wm_overlay_scale"))
    except AttributeError: pass
    try: w.wmOverlayMaxLabel.setText(_t("label.wm_overlay_max"))
    except AttributeError: pass
    try: w.wmOverlayMarginLabel.setText(_t("label.wm_overlay_margin"))
    except AttributeError: pass

    # ── Checkboxes ──
    try: w.runProcessCheckbox.setText(_t("label.postprocess_enabled"))
    except AttributeError: pass
    try: w.postprocessCompactCheckbox.setText(_t("label.postprocess_compact"))
    except AttributeError: pass
    try: w.runComicZipCheckbox.setText(_t("label.run_comiczip"))
    except AttributeError: pass
    try: w.parallelProcessingCheckbox.setText(_t("label.parallel_processing"))
    except AttributeError: pass
    try: w.watermarkFullpageEnabledCheckbox.setText(_t("label.wm_fullpage_enabled"))
    except AttributeError: pass
    try: w.watermarkFullpageInsertModeCheckbox.setText(_t("label.wm_insert_mode"))
    except AttributeError: pass
    try: w.watermarkOverlayEnabledCheckbox.setText(_t("label.wm_overlay_enabled"))
    except AttributeError: pass
    try: w.watermarkHeaderEnabledCheckbox.setText(_t("label.wm_header_enabled"))
    except AttributeError: pass
    try: w.watermarkFooterEnabledCheckbox.setText(_t("label.wm_footer_enabled"))
    except AttributeError: pass

    # ── Buttons ──
    try: w.browseButton.setText(_t("button.browse"))
    except AttributeError: pass
    try: w.typeButton.setText(_t("button.type"))
    except AttributeError: pass
    try: w.redrawButton.setText(_t("button.redraw"))
    except AttributeError: pass
    try: w.customButton.setText(_t("button.custom"))
    except AttributeError: pass
    try: w.dualButton.setText(_t("button.dual"))
    except AttributeError: pass
    try: w.presetsEditGroupBox.setTitle(_t("group.presets"))
    except AttributeError: pass
    try: w.presetsHintLabel.setText(_t("label.presets_hint"))
    except AttributeError: pass
    try: w.presetWidthLabel.setText(_t("label.preset_width"))
    except AttributeError: pass
    try: w.presetDetectorLabel.setText(_t("label.preset_detector"))
    except AttributeError: pass
    try: w.presetFormatLabel.setText(_t("label.preset_format"))
    except AttributeError: pass
    try: w.presetSplitLabel.setText(_t("label.preset_split"))
    except AttributeError: pass
    try: w.presetQualityLabel.setText(_t("label.preset_quality"))
    except AttributeError: pass
    try: w.presetSaveButton.setText(_t("button.preset_save"))
    except AttributeError: pass
    try: w.presetResetButton.setText(_t("button.preset_reset"))
    except AttributeError: pass
    try: w.presetApplyButton.setText(_t("button.preset_apply"))
    except AttributeError: pass
    try: w.installWaifu2xButton.setText(_t("button.install_waifu"))
    except AttributeError: pass
    try: w.startProcessButton.setText(_t("button.start"))
    except AttributeError: pass

    # ── Placeholder texts ──
    try: w.watermarkFullpagePathField.setPlaceholderText(_t("label.wm_fullpage_paths"))
    except AttributeError: pass
    try: w.watermarkOverlayPathField.setPlaceholderText(_t("label.wm_overlay_paths"))
    except AttributeError: pass
    try: w.watermarkHeaderPathField.setPlaceholderText(_t("label.wm_header_paths"))
    except AttributeError: pass
    try: w.watermarkFooterPathField.setPlaceholderText(_t("label.wm_footer_paths"))
    except AttributeError: pass

    # ── Combobox items ──
    try:
        cb = w.watermarkOverlayPositionCombo
        cb.setItemText(0, _t("combo.auto"))
        cb.setItemText(1, _t("combo.top_left"))
        cb.setItemText(2, _t("combo.top_right"))
        cb.setItemText(3, _t("combo.bottom_left"))
        cb.setItemText(4, _t("combo.bottom_right"))
        cb.setItemText(5, _t("combo.center"))
    except AttributeError:
        pass

    # ── Status field ──
    try:
        if w.statusField.text() in ("Idle", "Parado", ""):
            w.statusField.setText(_t("status.idle"))
    except AttributeError:
        pass


def initialize_gui(
    *,
    preset: str | None = None,
    input_path: str | None = None,
    waifu: bool = False,
    watermark: bool | None = None,
    autostart: bool = False,
) -> None:
    global _main_window, _settings, _process_thread
    global _folder_drop_filter, _auto_close_on_finish
    global _job_queue, _job_running

    _main_window = QUiLoader().load(os.path.join(_SCRIPT_DIR, "layout.ui"))
    _settings = SettingsHandler()

    _settings.save("postprocess_app", WAIFU_EXE_PATH)
    _settings.save("postprocess_args", WAIFU_ARGS_JPG)

    pixmap = QPixmap()
    pixmap.loadFromData(icon)
    _main_window.setWindowIcon(QIcon(pixmap))
    _main_window.setWindowTitle(f"{APP_NAME} By {APP_VENDOR} [{APP_VERSION}]")

    _on_load()
    _apply_translations()
    _bind_signals()
    try:
        _load_preset_editor()
    except Exception:
        pass

    _folder_drop_filter = FolderDropFilter(_main_window)
    _main_window.inputField.setAcceptDrops(True)
    _main_window.inputField.installEventFilter(_folder_drop_filter)

    _process_thread = ProcessThread(_main_window)
    _process_thread.progress.connect(_update_progress)
    _process_thread.postProcessConsole.connect(_update_console)
    _process_thread.showWarning.connect(
        lambda t, m: QMessageBox.warning(_main_window, t, m)
    )
    _process_thread.showError.connect(
        lambda t, m: QMessageBox.critical(_main_window, t, m)
    )
    _process_thread.showInfo.connect(
        lambda t, m: QMessageBox.information(_main_window, t, m)
    )
    _process_thread.finished.connect(_maybe_auto_close)
    _process_thread.finished.connect(_maybe_start_next_job)

    _auto_close_on_finish = bool(autostart)
    _job_queue = []
    _job_running = False

    _main_window.show()

    if AUTO_CHECK_UPDATES_ON_STARTUP:
        # Delay slightly so UI is visible/responsive before network call.
        QTimer.singleShot(1200, _startup_update_check)

    if preset or input_path or waifu or (watermark is not None) or autostart:
        _log.info("initialize_gui: calling enqueue_job preset=%s input=%s", preset, input_path)
        enqueue_job(
            preset=preset,
            input_path=input_path,
            waifu=waifu,
            watermark=watermark,
            autostart=autostart,
        )


def enqueue_job(
    *,
    preset: str | None = None,
    input_path: str | None = None,
    waifu: bool = False,
    watermark: bool | None = None,
    autostart: bool = True,
) -> None:
    global _auto_close_on_finish
    if _main_window is None:
        _log.warning("enqueue_job: _main_window is None, skipping")
        return

    _log.info(
        "Job enqueued: preset=%s input=%s waifu=%s autostart=%s",
        preset, input_path, waifu, autostart,
    )

    _job_queue.append(
        {
            "preset": preset,
            "input_path": input_path,
            "waifu": waifu,
            "watermark": watermark,
            "autostart": autostart,
        }
    )

    if autostart:
        _auto_close_on_finish = True

    if not _job_running:
        _start_next_job()
    else:
        _log.info("Job queued (another job is running, %d in queue)", len(_job_queue))


def _start_next_job() -> None:
    global _job_running
    if not _job_queue:
        _job_running = False
        return

    job = _job_queue.pop(0)
    _job_running = True

    preset = job.get("preset")
    input_path = job.get("input_path")
    waifu = bool(job.get("waifu", False))
    watermark = job.get("watermark", None)
    autostart = bool(job.get("autostart", True))

    if preset:
        preset_lower = str(preset).strip().lower()
        if preset_lower == "type":
            _apply_type_preset()
        elif preset_lower == "redraw":
            _apply_redraw_preset()
        elif preset_lower in {"dual", "duplo", "dupla"}:
            _apply_dual_preset()
        elif preset_lower in {"custom", "personalizado", "personalizada"}:
            _apply_custom_preset()

    _settings.save("run_postprocess", waifu)
    _main_window.runProcessCheckbox.setChecked(waifu)

    if watermark is not None:
        _set_watermark_enabled(bool(watermark))

    if input_path:
        _main_window.inputField.setText(input_path)

    if autostart:
        QTimer.singleShot(0, _launch_process)


def _maybe_start_next_job() -> None:
    if _job_queue:
        QTimer.singleShot(0, _start_next_job)


def _maybe_auto_close() -> None:
    if _auto_close_on_finish and not _job_queue:
        QTimer.singleShot(250, QApplication.quit)


def _startup_update_check() -> None:
    # Startup check intentionally disabled: updates are manual via button.
    return


def _is_any_watermark_enabled() -> bool:
    return any(bool(_settings.load(key)) for key in _WATERMARK_KEYS)


def _watermark_context_action_label() -> str:
    return (
        "Desativar Marcas d'agua"
        if _is_any_watermark_enabled()
        else "Ativar Marcas d'agua"
    )


def _set_watermark_enabled(enabled: bool) -> None:
    _settings.save("watermark_fullpage_enabled", enabled)
    _settings.save("watermark_overlay_enabled", enabled)
    _settings.save("watermark_header_enabled", enabled)
    _settings.save("watermark_footer_enabled", enabled)

    _main_window.watermarkFullpageEnabledCheckbox.setChecked(enabled)
    _main_window.watermarkOverlayEnabledCheckbox.setChecked(enabled)
    _main_window.watermarkHeaderEnabledCheckbox.setChecked(enabled)
    _main_window.watermarkFooterEnabledCheckbox.setChecked(enabled)

    _toggle_fullpage_options(enabled)
    _toggle_overlay_options(enabled)
    _toggle_header_options(enabled)
    _toggle_footer_options(enabled)


def _toggle_fullpage_options(enabled: bool) -> None:
    """Show/hide fullpage watermark options based on checkbox state."""

    def set_layout_visible(layout, visible, exclude_widget):
        """Recursively set visibility for all widgets in layout."""
        if not layout:
            return
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item:
                widget = item.widget()
                sublayout = item.layout()
                if widget and widget != exclude_widget:
                    widget.setVisible(visible)
                if sublayout:
                    set_layout_visible(sublayout, visible, exclude_widget)

    layout = _main_window.watermarkFullpageGroupBox.layout()
    set_layout_visible(layout, enabled, _main_window.watermarkFullpageEnabledCheckbox)


def _toggle_overlay_options(enabled: bool) -> None:
    """Show/hide overlay watermark options based on checkbox state."""

    def set_layout_visible(layout, visible, exclude_widget):
        """Recursively set visibility for all widgets in layout."""
        if not layout:
            return
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item:
                widget = item.widget()
                sublayout = item.layout()
                if widget and widget != exclude_widget:
                    widget.setVisible(visible)
                if sublayout:
                    set_layout_visible(sublayout, visible, exclude_widget)

    layout = _main_window.watermarkOverlayGroupBox.layout()
    set_layout_visible(layout, enabled, _main_window.watermarkOverlayEnabledCheckbox)


def _toggle_header_options(enabled: bool) -> None:
    """Show/hide header watermark options based on checkbox state."""
    _main_window.watermarkHeaderPathField.setVisible(enabled)
    _main_window.browseWatermarkHeaderButton.setVisible(enabled)


def _toggle_footer_options(enabled: bool) -> None:
    """Show/hide footer watermark options based on checkbox state."""
    _main_window.watermarkFooterPathField.setVisible(enabled)
    _main_window.browseWatermarkFooterButton.setVisible(enabled)


_PRESET_EDITOR_NAMES = ("type", "redraw", "custom", "dual")

_PRESET_FORMAT_OPTIONS = (".jpg", ".png", ".webp", ".avif", DUAL_OUTPUT_TYPE)


def _preset_format_to_combo_idx(output_type: str, extra: list | None = None) -> int:
    """Map stored output_type (+extras) to the editor/format combo index."""
    raw = str(output_type or "").strip().lower().replace(";", "+").replace(",", "+")
    parts = [p.strip() for p in raw.split("+") if p.strip()]
    norm = [(p if p.startswith(".") else f".{p}") for p in parts]
    extra_norm = [str(e or "").strip().lower() for e in (extra or []) if str(e or "").strip()]
    if ".webp" in norm and ".png" in (norm[1:] + extra_norm):
        return 4
    return _output_type_to_custom_format(norm[0] if norm else ".jpg")


def _combo_idx_to_preset_format(idx: int) -> tuple[str, list]:
    """Combo index → (output_type, extra_output_types) for presets."""
    idx = int(idx)
    if idx == 4:
        return DUAL_OUTPUT_TYPE, [".png"]
    single = _custom_format_to_output_type(idx)
    return single, []


def _waifu_args_for_output(output_type: str) -> str:
    primary = str(output_type or "").replace(";", "+").replace(",", "+").split("+")[0].strip().lower()
    if not primary.startswith("."):
        primary = f".{primary}"
    mapping = {".jpg": WAIFU_ARGS_JPG, ".jpeg": WAIFU_ARGS_JPG, ".png": WAIFU_ARGS_PNG,
               ".webp": WAIFU_ARGS_WEBP, ".avif": WAIFU_ARGS_AVIF}
    return mapping.get(primary, WAIFU_ARGS_JPG)


def _custom_format_to_output_type(idx: int) -> str:
    mapping = {0: ".jpg", 1: ".png", 2: ".webp", 3: ".avif"}
    return mapping.get(int(idx), ".jpg")


def _load_int(key: str, default: int) -> int:
    """Load an int setting, falling back only when the value is None/invalid.

    Unlike ``value or default``, this preserves ``0`` (a valid value, e.g.
    detector_type=0 for Direct).
    """
    value = _settings.load(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _output_type_to_custom_format(output_type: str) -> int:
    mapping = {".jpg": 0, ".jpeg": 0, ".png": 1, ".webp": 2, ".avif": 3}
    raw = str(output_type or "").strip().lower().replace(";", "+").replace(",", "+")
    first = raw.split("+")[0].strip() if raw else ""
    return mapping.get(first, 0)


def _on_load() -> None:
    _main_window.statusField.setText("Idle")
    _main_window.statusProgressBar.setValue(0)
    _main_window.heightField.setValue(_settings.load("split_height"))
    # Custom preset controls
    try:
        _main_window.customWidthSpin.setValue(_load_int("enforce_width", 800))
    except Exception:
        pass
    try:
        _main_window.customDetectorCombo.setCurrentIndex(_load_int("detector_type", 1))
    except Exception:
        pass
    try:
        _main_window.customFormatCombo.setCurrentIndex(
            _output_type_to_custom_format(str(_settings.load("output_type") or ".jpg"))
        )
    except Exception:
        pass
    _main_window.runProcessCheckbox.setChecked(_settings.load("run_postprocess"))
    _main_window.postprocessCompactCheckbox.setChecked(
        _settings.load("postprocess_compact_enabled")
    )
    _main_window.runComicZipCheckbox.setChecked(_settings.load("run_comiczip"))
    _main_window.parallelProcessingCheckbox.setChecked(
        _settings.load("parallel_processing")
    )
    # Watermark settings
    _main_window.watermarkFullpageEnabledCheckbox.setChecked(
        _settings.load("watermark_fullpage_enabled")
    )
    _main_window.watermarkFullpagePathField.setText(
        _settings.load("watermark_fullpage_paths")
    )
    _main_window.watermarkFullpageThresholdSpin.setValue(
        _settings.load("watermark_fullpage_threshold")
    )
    _main_window.watermarkFullpageMaxSpin.setValue(
        _settings.load("watermark_fullpage_max_per_page")
    )
    _main_window.watermarkFullpageInsertModeCheckbox.setChecked(
        _settings.load("watermark_fullpage_insert_mode")
    )
    _main_window.watermarkFullpageMinAreaSpin.setValue(
        _settings.load("watermark_fullpage_min_area_height")
    )
    _main_window.watermarkFullpageSpacingTopSpin.setValue(
        _settings.load("watermark_fullpage_min_spacing_top")
    )
    _main_window.watermarkFullpageSpacingBottomSpin.setValue(
        _settings.load("watermark_fullpage_min_spacing_bottom")
    )
    _main_window.watermarkChapterSkipSpin.setValue(
        _settings.load("watermark_chapter_skip")
    )
    _main_window.watermarkOverlayEnabledCheckbox.setChecked(
        _settings.load("watermark_overlay_enabled")
    )
    _main_window.watermarkOverlayPathField.setText(
        _settings.load("watermark_overlay_paths")
    )
    _main_window.watermarkOverlayPositionCombo.setCurrentIndex(
        _settings.load("watermark_overlay_position")
    )
    _main_window.watermarkOverlayOpacitySpin.setValue(
        _settings.load("watermark_overlay_opacity")
    )
    _main_window.watermarkOverlayScaleSpin.setValue(
        _settings.load("watermark_overlay_scale_pct")
    )
    _main_window.watermarkOverlayMaxSpin.setValue(
        _settings.load("watermark_overlay_max_per_page")
    )
    _main_window.watermarkOverlayMarginSpin.setValue(
        _settings.load("watermark_overlay_margin")
    )
    _main_window.watermarkHeaderEnabledCheckbox.setChecked(
        _settings.load("watermark_header_enabled")
    )
    _main_window.watermarkHeaderPathField.setText(
        _settings.load("watermark_header_paths")
    )
    _main_window.watermarkFooterEnabledCheckbox.setChecked(
        _settings.load("watermark_footer_enabled")
    )
    _main_window.watermarkFooterPathField.setText(
        _settings.load("watermark_footer_paths")
    )

    # Initialize visibility based on checkbox states
    _toggle_fullpage_options(_settings.load("watermark_fullpage_enabled"))
    _toggle_overlay_options(_settings.load("watermark_overlay_enabled"))
    _toggle_header_options(_settings.load("watermark_header_enabled"))
    _toggle_footer_options(_settings.load("watermark_footer_enabled"))


def _bind_signals() -> None:
    w = _main_window
    w.inputField.textChanged.connect(_input_field_changed)
    w.browseButton.clicked.connect(_browse_location)
    w.heightField.valueChanged.connect(
        lambda: _settings.save("split_height", w.heightField.value())
    )
    w.runProcessCheckbox.stateChanged.connect(
        lambda: _settings.save("run_postprocess", w.runProcessCheckbox.isChecked())
    )
    w.postprocessCompactCheckbox.stateChanged.connect(
        lambda: _settings.save(
            "postprocess_compact_enabled", w.postprocessCompactCheckbox.isChecked()
        )
    )
    w.runComicZipCheckbox.stateChanged.connect(
        lambda: _settings.save("run_comiczip", w.runComicZipCheckbox.isChecked())
    )
    w.parallelProcessingCheckbox.stateChanged.connect(
        lambda: _settings.save(
            "parallel_processing", w.parallelProcessingCheckbox.isChecked()
        )
    )
    w.installWaifu2xButton.clicked.connect(lambda: _waifu2x_action(repair=False))
    w.repairWaifu2xButton.clicked.connect(lambda: _waifu2x_action(repair=True))
    w.installContextMenuButton.clicked.connect(_install_context_menu)
    w.removeContextMenuButton.clicked.connect(_remove_context_menu)
    w.typeButton.clicked.connect(_apply_type_preset)
    w.redrawButton.clicked.connect(_apply_redraw_preset)
    try:
        w.dualButton.clicked.connect(_apply_dual_preset)
    except AttributeError:
        pass
    w.customButton.clicked.connect(_apply_custom_preset)
    # Presets editor tab
    try:
        w.presetEditSelector.currentIndexChanged.connect(_load_preset_editor)
    except AttributeError:
        pass
    try:
        w.presetSaveButton.clicked.connect(_save_preset_editor)
    except AttributeError:
        pass
    try:
        w.presetResetButton.clicked.connect(_reset_preset_editor)
    except AttributeError:
        pass
    try:
        w.presetApplyButton.clicked.connect(_apply_preset_editor)
    except AttributeError:
        pass
    # Custom preset controls — save immediately so context-menu Personalizado uses them
    w.customWidthSpin.valueChanged.connect(
        lambda v: [_settings.save("enforce_type", 2), _settings.save("enforce_width", int(v))]
    )
    w.customDetectorCombo.currentIndexChanged.connect(
        lambda idx: _settings.save("detector_type", int(idx))
    )
    w.customFormatCombo.currentIndexChanged.connect(
        lambda idx: [
            _settings.save("output_type", _custom_format_to_output_type(int(idx))),
            _settings.save(
                "postprocess_args",
                {
                    0: WAIFU_ARGS_JPG,
                    1: WAIFU_ARGS_PNG,
                    2: WAIFU_ARGS_WEBP,
                    3: WAIFU_ARGS_AVIF,
                }.get(int(idx), WAIFU_ARGS_JPG),
            ),
        ]
    )
    w.startProcessButton.clicked.connect(_launch_process)
    w.updateAppButton.clicked.connect(
        lambda: _check_for_updates(silent_if_latest=False, auto_update=False)
    )
    # Watermark signals
    w.watermarkFullpageEnabledCheckbox.stateChanged.connect(
        lambda: [
            _settings.save(
                "watermark_fullpage_enabled",
                w.watermarkFullpageEnabledCheckbox.isChecked(),
            ),
            _toggle_fullpage_options(w.watermarkFullpageEnabledCheckbox.isChecked()),
        ]
    )
    w.watermarkFullpagePathField.textChanged.connect(
        lambda: _settings.save(
            "watermark_fullpage_paths", w.watermarkFullpagePathField.text()
        )
    )
    w.watermarkFullpageThresholdSpin.valueChanged.connect(
        lambda val: _settings.save("watermark_fullpage_threshold", val)
    )
    w.watermarkFullpageMaxSpin.valueChanged.connect(
        lambda val: _settings.save("watermark_fullpage_max_per_page", val)
    )
    w.watermarkFullpageInsertModeCheckbox.stateChanged.connect(
        lambda: _settings.save(
            "watermark_fullpage_insert_mode",
            w.watermarkFullpageInsertModeCheckbox.isChecked(),
        )
    )
    w.watermarkFullpageMinAreaSpin.valueChanged.connect(
        lambda val: _settings.save("watermark_fullpage_min_area_height", val)
    )
    w.watermarkFullpageSpacingTopSpin.valueChanged.connect(
        lambda val: _settings.save("watermark_fullpage_min_spacing_top", val)
    )
    w.watermarkFullpageSpacingBottomSpin.valueChanged.connect(
        lambda val: _settings.save("watermark_fullpage_min_spacing_bottom", val)
    )
    w.watermarkChapterSkipSpin.valueChanged.connect(
        lambda val: _settings.save("watermark_chapter_skip", val)
    )
    w.watermarkOverlayEnabledCheckbox.stateChanged.connect(
        lambda: [
            _settings.save(
                "watermark_overlay_enabled",
                w.watermarkOverlayEnabledCheckbox.isChecked(),
            ),
            _toggle_overlay_options(w.watermarkOverlayEnabledCheckbox.isChecked()),
        ]
    )
    w.watermarkOverlayPathField.textChanged.connect(
        lambda: _settings.save(
            "watermark_overlay_paths", w.watermarkOverlayPathField.text()
        )
    )
    w.watermarkOverlayPositionCombo.currentIndexChanged.connect(
        lambda idx: _settings.save("watermark_overlay_position", idx)
    )
    w.watermarkOverlayOpacitySpin.valueChanged.connect(
        lambda val: _settings.save("watermark_overlay_opacity", val)
    )
    w.watermarkOverlayScaleSpin.valueChanged.connect(
        lambda val: _settings.save("watermark_overlay_scale_pct", val)
    )
    w.watermarkOverlayMaxSpin.valueChanged.connect(
        lambda val: _settings.save("watermark_overlay_max_per_page", val)
    )
    w.watermarkOverlayMarginSpin.valueChanged.connect(
        lambda val: _settings.save("watermark_overlay_margin", val)
    )
    w.watermarkHeaderEnabledCheckbox.stateChanged.connect(
        lambda: [
            _settings.save(
                "watermark_header_enabled", w.watermarkHeaderEnabledCheckbox.isChecked()
            ),
            _toggle_header_options(w.watermarkHeaderEnabledCheckbox.isChecked()),
        ]
    )
    w.watermarkHeaderPathField.textChanged.connect(
        lambda: _settings.save(
            "watermark_header_paths", w.watermarkHeaderPathField.text()
        )
    )
    w.watermarkFooterEnabledCheckbox.stateChanged.connect(
        lambda: [
            _settings.save(
                "watermark_footer_enabled", w.watermarkFooterEnabledCheckbox.isChecked()
            ),
            _toggle_footer_options(w.watermarkFooterEnabledCheckbox.isChecked()),
        ]
    )
    w.watermarkFooterPathField.textChanged.connect(
        lambda: _settings.save(
            "watermark_footer_paths", w.watermarkFooterPathField.text()
        )
    )
    w.browseWatermarkFullpageButton.clicked.connect(
        lambda: _browse_images(w.watermarkFullpagePathField)
    )
    w.browseWatermarkOverlayButton.clicked.connect(
        lambda: _browse_images(w.watermarkOverlayPathField)
    )
    w.browseWatermarkHeaderButton.clicked.connect(
        lambda: _browse_images(w.watermarkHeaderPathField)
    )
    w.browseWatermarkFooterButton.clicked.connect(
        lambda: _browse_images(w.watermarkFooterPathField)
    )


def _browse_images(target_field) -> None:
    """Open a file dialog to select one or more image files and append to the target field."""
    files, _ = QFileDialog.getOpenFileNames(
        _main_window,
        "Selecionar imagens",
        os.path.expanduser("~"),
        "Images (*.png *.svg *.avif *.jpg *.jpeg *.webp *.bmp)",
    )
    if files:
        existing = (target_field.text() or "").strip()
        paths = [p for p in existing.split(";") if p.strip()] if existing else []
        paths.extend(files)
        target_field.setText(";".join(paths))


def _input_field_changed() -> None:
    path = (_main_window.inputField.text() or "").strip()
    if path and os.path.exists(path):
        _settings.save("last_browse_location", path)


def _browse_location() -> None:
    start = _settings.load("last_browse_location")
    if not start or not os.path.exists(start):
        start = os.path.expanduser("~")
    dialog = QFileDialog(_main_window, "Select Input Directory Files", start)
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        selected = dialog.selectedFiles()[0] or ""
        _main_window.inputField.setText(selected)


def _save_preset(values: dict) -> None:
    """Persist a dict of setting key→value pairs and sync the height spinner.

    Also syncs the custom-preset controls so they always reflect the applied
    preset (avoids the controls silently drifting from the real settings when
    Type/Redraw is used, which made a later "Personalizado" click look like it
    swapped the user's choices).
    """
    for key, val in values.items():
        _settings.save(key, val)
    if "split_height" in values:
        _main_window.heightField.setValue(values["split_height"])
    try:
        if "enforce_width" in values:
            _main_window.customWidthSpin.setValue(int(values["enforce_width"]))
        if "detector_type" in values:
            _main_window.customDetectorCombo.setCurrentIndex(
                int(values["detector_type"])
            )
        if "output_type" in values:
            _main_window.customFormatCombo.setCurrentIndex(
                _output_type_to_custom_format(str(values["output_type"]))
            )
    except Exception:
        pass


def _apply_named_preset(name: str) -> None:
    """Apply an editable preset (Type/Redraw/Custom/Dual) to live settings."""
    applied = apply_preset_to_settings(_settings, name)
    # Keep waifu args in sync when the stored preset didn't carry custom args.
    preset = get_preset(_settings, name)
    if not preset.get("postprocess_args"):
        applied["postprocess_args"] = _waifu_args_for_output(
            str(applied.get("output_type", ".jpg"))
        )
        _settings.save("postprocess_args", applied["postprocess_args"])
    _save_preset(applied)


def _apply_type_preset() -> None:
    _apply_named_preset("type")


def _apply_redraw_preset() -> None:
    _apply_named_preset("redraw")


def _apply_dual_preset() -> None:
    """Dual: processa 1x, salva 01.webp + 01.png na mesma pasta."""
    _apply_named_preset("dual")


def _apply_custom_preset() -> None:
    """Personalizado: usa largura/detector/formato configurados nos controles custom."""
    if _main_window is not None:
        try:
            width = int(_main_window.customWidthSpin.value())
            detector = int(_main_window.customDetectorCombo.currentIndex())
            fmt_idx_out = int(_main_window.customFormatCombo.currentIndex())
        except Exception:
            width = _load_int("enforce_width", 800)
            detector = _load_int("detector_type", 1)
            fmt_idx_out = _output_type_to_custom_format(str(_settings.load("output_type") or ".jpg"))
    else:
        width = _load_int("enforce_width", 800)
        detector = _load_int("detector_type", 1)
        fmt_idx_out = _output_type_to_custom_format(str(_settings.load("output_type") or ".jpg"))

    output_type = _custom_format_to_output_type(fmt_idx_out)

    # Persist quick custom choices so context-menu Personalizado uses them.
    save_preset(
        _settings,
        "custom",
        {
            "output_type": output_type,
            "extra_output_types": [],
            "enforce_width": width,
            "detector_type": detector,
            "split_height": int(_main_window.heightField.value())
            if _main_window is not None
            else _load_int("split_height", 15000),
            "lossy_quality": 100,
            "postprocess_args": _waifu_args_for_output(output_type),
        },
    )
    _apply_named_preset("custom")


def _selected_preset_editor_name() -> str:
    try:
        idx = int(_main_window.presetEditSelector.currentIndex())
    except Exception:
        idx = 2
    if 0 <= idx < len(_PRESET_EDITOR_NAMES):
        return _PRESET_EDITOR_NAMES[idx]
    return "custom"


def _load_preset_editor(*_args) -> None:
    """Fill the Presets-tab editor with the selected stored preset."""
    if _main_window is None or _settings is None:
        return
    name = _selected_preset_editor_name()
    try:
        preset = get_preset(_settings, name)
    except Exception:
        return
    try:
        _main_window.presetWidthSpin.setValue(int(preset.get("enforce_width", 800)))
    except Exception:
        pass
    try:
        _main_window.presetDetectorCombo.setCurrentIndex(
            int(preset.get("detector_type", 1))
        )
    except Exception:
        pass
    try:
        _main_window.presetFormatCombo.setCurrentIndex(
            _preset_format_to_combo_idx(
                str(preset.get("output_type", ".jpg")),
                preset.get("extra_output_types") or [],
            )
        )
    except Exception:
        pass
    try:
        _main_window.presetSplitSpin.setValue(int(preset.get("split_height", 15000)))
    except Exception:
        pass
    try:
        _main_window.presetQualitySpin.setValue(int(preset.get("lossy_quality", 100)))
    except Exception:
        pass


def _collect_preset_editor_values() -> dict:
    fmt_idx = int(_main_window.presetFormatCombo.currentIndex())
    output_type, extras = _combo_idx_to_preset_format(fmt_idx)
    detector = int(_main_window.presetDetectorCombo.currentIndex())
    values: dict = {
        "output_type": output_type,
        "extra_output_types": extras,
        "enforce_width": int(_main_window.presetWidthSpin.value()),
        "detector_type": detector,
        "split_height": int(_main_window.presetSplitSpin.value()),
        "lossy_quality": int(_main_window.presetQualitySpin.value()),
        "postprocess_args": _waifu_args_for_output(output_type),
    }
    if detector == 1:
        values.update({"sensitivity": 100, "scan_step": 30, "ignorable_pixels": 0})
    return values


def _save_preset_editor() -> None:
    if _main_window is None or _settings is None:
        return
    name = _selected_preset_editor_name()
    try:
        save_preset(_settings, name, _collect_preset_editor_values())
    except Exception as exc:
        try:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(_main_window, "Presets", str(exc))
        except Exception:
            pass


def _reset_preset_editor() -> None:
    if _main_window is None or _settings is None:
        return
    name = _selected_preset_editor_name()
    try:
        reset_preset(_settings, name)
    except Exception:
        pass
    _load_preset_editor()


def _apply_preset_editor() -> None:
    _save_preset_editor()
    name = _selected_preset_editor_name()
    try:
        _apply_named_preset(name)
    except Exception as exc:
        try:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(_main_window, "Presets", str(exc))
        except Exception:
            pass


def _download_and_extract_waifu2x(*, repair: bool) -> None:
    installed = download_and_extract_waifu2x(repair=repair)
    _settings.save("postprocess_app", installed)


def _waifu2x_action(*, repair: bool) -> None:
    label = "Reparado" if repair else "Instalado com sucesso!"
    try:
        _download_and_extract_waifu2x(repair=repair)
        QMessageBox.information(_main_window, "Waifu2X", f"Waifu2X {label}")
    except Exception:
        action = "reparar" if repair else "instalar"
        QMessageBox.critical(_main_window, "Waifu2X", f"Falha ao {action}")


def _pythonw_path() -> str:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        pythonw = exe[: -len("python.exe")] + "pythonw.exe"
        if os.path.isfile(pythonw):
            return pythonw
    return exe


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _set_reg_command(
    root_path: str,
    name: str,
    command: str,
    icon_val: str | None = None,
) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root_path) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, name)
        if icon_val:
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon_val)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root_path + "\\command") as cmd_key:
        winreg.SetValueEx(cmd_key, None, 0, winreg.REG_SZ, command)


def _build_toggle_command(base_exe: str, flag: str) -> str:
    """Build command for toggle entries (launch GUI, no input path needed)."""
    if not _is_frozen():
        # Use GUI script instead of context menu script
        pythonw = _pythonw_path()
        script = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "SmartStitchGUI.py"))
        return f'"{pythonw}" "{script}" --{flag}'
    return f'"{base_exe}" --{flag}'


def _build_context_command(
    base_exe: str,
    preset: str | None,
    waifu: bool,
    watermark: bool | None,
    autostart: bool,
    include_input: bool,
    toggle_watermark: bool,
    is_zip: bool = False,
    zip_cleanup: bool = False,
) -> str:
    parts = [f'"{base_exe}"']
    if include_input:
        if is_zip:
            parts.append('--from-zip --input "%1"')
        else:
            parts.append('--input "%V"')
    if preset:
        parts.append(f"--preset {preset}")
    if waifu:
        parts.append("--waifu")
    if toggle_watermark:
        parts.append("--toggle-watermark")
    if watermark is not None:
        parts.append(f"--set-watermark {'on' if watermark else 'off'}")
    if autostart:
        parts.append("--autostart")
    if zip_cleanup:
        parts.append("--zip-cleanup")
    elif not toggle_watermark and preset:
        if _read_registry_toggle("ZipCleanup"):
            parts.append("--zip-cleanup")
    return " ".join(parts)


def _read_registry_toggle(name: str) -> bool:
    """Read toggle state from Registry."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_TOGGLE_KEY, 0, winreg.KEY_READ
        ) as k:
            val, _ = winreg.QueryValueEx(k, name)
            return bool(val)
    except FileNotFoundError:
        return False


def _write_registry_toggle(name: str, value: bool) -> None:
    """Write toggle state to Registry."""
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_TOGGLE_KEY)
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
        winreg.CloseKey(key)
    except Exception:
        pass


def _toggle_label(name: str, label_prefix: str) -> str:
    """Return toggle label with current state indicator."""
    state = _read_registry_toggle(name)
    icon = "ON" if state else "OFF"
    return f"{label_prefix}: {icon}"


def _install_context_menu() -> None:
    try:
        icon_val: str | None = None
        if _is_frozen():
            base_exe = sys.executable
            icon_val = f"{base_exe},0"
        else:
            if not os.path.isfile(_CONTEXT_MENU_GUI):
                raise FileNotFoundError(f"Missing GUI script: {_CONTEXT_MENU_GUI}")
            python = _pythonw_path()
            base_exe = f'{python}" "{_CONTEXT_MENU_GUI}'
            if os.path.isfile(_ICON_FILE):
                icon_val = _ICON_FILE

        # Cleanup old product naming to avoid duplicate context menus.
        for legacy_key in _LEGACY_REG_BASE_KEYS:
            _delete_reg_tree(winreg.HKEY_CURRENT_USER, legacy_key)

        # Cleanup COM ShellEx handlers from the failed C++ DLL experiment.
        for com_key in _COM_SHELLEX_KEYS:
            _delete_reg_tree(winreg.HKEY_CURRENT_USER, com_key)

        # Install for folders (first 2 keys use _CONTEXT_MENU_ENTRIES)
        for base in _REG_BASE_KEYS[:2]:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, APP_NAME)
                winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
                if icon_val:
                    winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon_val)

            _delete_reg_tree(winreg.HKEY_CURRENT_USER, base + "\\shell")

            for (
                reg_name,
                label,
                preset,
                waifu,
                watermark,
                autostart,
                include_input,
                is_zip,
                zip_cleanup,
            ) in _CONTEXT_MENU_ENTRIES:
                if reg_name == "ToggleCompact":
                    effective_label = _toggle_label("ZipCleanup", "Compactar")
                    cmd = _build_toggle_command(base_exe, "toggle-zip")
                elif reg_name in ("Sep1", "Sep2"):
                    # Separator entries: set label to empty, skip
                    # Shell separators are automatic if label is empty and no command
                    # Actually, SubCommands don't support separators easily — just skip
                    continue
                elif reg_name == "WatermarkToggle":
                    effective_label = _watermark_context_action_label()
                    cmd = _build_context_command(
                        base_exe, preset, waifu, watermark, autostart,
                        include_input, True, is_zip, zip_cleanup,
                    )
                else:
                    effective_label = label
                    cmd = _build_context_command(
                        base_exe, preset, waifu, watermark, autostart,
                        include_input, False, is_zip, zip_cleanup,
                    )
                _set_reg_command(
                    base + "\\shell\\" + reg_name,
                    effective_label,
                    cmd,
                    icon_val,
                )

        # Install for .zip/.cbz files (last 2 keys use _ZIP_CONTEXT_MENU_ENTRIES)
        for base in _REG_BASE_KEYS[2:]:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, APP_NAME)
                winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
                if icon_val:
                    winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon_val)

            _delete_reg_tree(winreg.HKEY_CURRENT_USER, base + "\\shell")

            for (
                reg_name,
                label,
                preset,
                waifu,
                watermark,
                autostart,
                include_input,
                is_zip,
                zip_cleanup,
            ) in _ZIP_CONTEXT_MENU_ENTRIES:
                if reg_name == "ToggleCompact":
                    effective_label = _toggle_label("ZipCleanup", "Compactar")
                    cmd = _build_toggle_command(base_exe, "toggle-zip")
                elif reg_name == "Sep1":
                    continue
                else:
                    effective_label = label
                    cmd = _build_context_command(
                        base_exe, preset, waifu, watermark, autostart,
                        include_input, False, is_zip, zip_cleanup,
                    )
                _set_reg_command(
                    base + "\\shell\\" + reg_name,
                    effective_label,
                    cmd,
                    icon_val,
                )

        QMessageBox.information(
            _main_window,
            APP_NAME,
            "Menu de contexto instalado!\n\n"
            "Clique com o botão direito em uma pasta para ver as opções.",
        )
    except Exception as exc:
        QMessageBox.critical(
            _main_window,
            APP_NAME,
            f"Falha ao adicionar no Registro: {exc}",
        )


def _delete_reg_tree(root: int, sub_key: str) -> None:
    """Recursively delete a registry key tree (best-effort)."""
    try:
        with winreg.OpenKey(root, sub_key, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            while True:
                try:
                    child = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_reg_tree(root, sub_key + "\\" + child)
    except (FileNotFoundError, PermissionError, OSError):
        return
    try:
        winreg.DeleteKey(root, sub_key)
    except (FileNotFoundError, PermissionError, OSError):
        pass


def _remove_context_menu() -> None:
    try:
        for key in (*_REG_BASE_KEYS, *_LEGACY_REG_BASE_KEYS, *_COM_SHELLEX_KEYS):
            _delete_reg_tree(winreg.HKEY_CURRENT_USER, key)
        QMessageBox.information(
            _main_window,
            APP_NAME,
            "Menu de contexto removido!",
        )
    except Exception as exc:
        QMessageBox.critical(
            _main_window,
            APP_NAME,
            f"Falha ao remover do Registro: {exc}",
        )


def _update_progress(percentage: int, message: str) -> None:
    _main_window.statusField.setText(message)
    _main_window.statusProgressBar.setValue(percentage)


def _update_console(message: str) -> None:
    _main_window.processConsoleField.append(message)


def _version_tuple(version: str) -> tuple[int, ...]:
    cleaned = (version or "").strip()
    if cleaned.lower().startswith("v"):
        cleaned = cleaned[1:]
    parts = [int(part) for part in re.findall(r"\d+", cleaned)]
    if not parts:
        return (0,)
    return tuple(parts)


def _fetch_latest_release() -> dict:
    safe_api_url = _assert_safe_http_url(
        GITHUB_API_LATEST_URL, allowed_hosts=_SAFE_UPDATE_HOSTS
    )
    request = urllib.request.Request(
        safe_api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": APP_NAME,
        },
    )

    with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
        payload = response.read().decode("utf-8")
        return json.loads(payload)


def _pick_release_zip_asset_url(release_data: dict) -> str | None:
    assets = release_data.get("assets") or []
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        url = str(asset.get("browser_download_url") or "").strip()
        if name.endswith(".zip") and url:
            return _assert_safe_http_url(url, allowed_hosts=_SAFE_UPDATE_HOSTS)
    return None


def _download_file(
    url: str,
    target_path: str,
    progress_callback: Callable[[int, int], None] | None = None,
) -> None:
    safe_url = _assert_safe_http_url(url, allowed_hosts=_SAFE_UPDATE_HOSTS)
    request = urllib.request.Request(
        safe_url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": APP_NAME,
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
        total_bytes = 0
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                total_bytes = int(content_length)
            except (TypeError, ValueError):
                total_bytes = 0

        bytes_read = 0
        with open(target_path, "wb") as out:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                bytes_read += len(chunk)
                if progress_callback:
                    progress_callback(bytes_read, total_bytes)


def _resolve_payload_dir(payload_dir: str, exe_name: str) -> str:
    """Find extracted payload root that actually contains the target executable."""
    direct_exe = os.path.join(payload_dir, exe_name)
    if os.path.isfile(direct_exe):
        return payload_dir

    candidates: list[tuple[int, str]] = []
    for root, _, files in os.walk(payload_dir):
        if exe_name in files:
            rel = os.path.relpath(root, payload_dir)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            candidates.append((depth, root))

    if not candidates:
        return payload_dir

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _run_external_updater(
    *, staged_dir: str, payload_dir: str, app_dir: str, exe_name: str
) -> None:
    updater_cmd = os.path.join(staged_dir, "apply_update.cmd")
    current_pid = os.getpid()

    lines = [
        "@echo off",
        "setlocal",
        f"set TARGET_PID={current_pid}",
        f"set STAGED_DIR={staged_dir}",
        f"set PAYLOAD_DIR={payload_dir}",
        f"set APP_DIR={app_dir}",
        f"set EXE_NAME={exe_name}",
        "echo Aguardando app fechar...",
        ":wait_loop",
        'tasklist /FI "PID eq %TARGET_PID%" | findstr /I "%TARGET_PID%" >nul',
        "if %ERRORLEVEL%==0 (",
        "  timeout /t 1 /nobreak >nul",
        "  goto wait_loop",
        ")",
        "echo Aplicando arquivos de atualizacao...",
        'robocopy "%PAYLOAD_DIR%" "%APP_DIR%" /E /R:10 /W:2 /NP',
        "if %ERRORLEVEL% GEQ 8 (",
        "  echo Erro ao copiar arquivos. Iniciando versao anterior...",
        "  goto start_old",
        ")",
        "echo Atualizacao concluida! Iniciando novo version...",
        'if exist "%APP_DIR%\\%EXE_NAME%" (',
        '  start "" "%APP_DIR%\\%EXE_NAME%"',
        ")",
        "goto cleanup",
        ":start_old",
        'if exist "%APP_DIR%\\%EXE_NAME%" start "" "%APP_DIR%\\%EXE_NAME%"',
        ":cleanup",
        "echo Limpando arquivos temporarios...",
        "timeout /t 2 /nobreak >nul",
        'rmdir /s /q "%STAGED_DIR%" 2>nul',
        "echo Atualizacao finalizada.",
    ]
    with open(updater_cmd, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\r\n".join(lines) + "\r\n")

    subprocess.Popen(  # nosec B603 B607
        ["cmd", "/c", updater_cmd],
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True,
    )


def _self_update_from_release(release_data: dict) -> tuple[bool, str]:
    if not _is_frozen():
        return (
            False,
            "Autoatualizacao automatica so esta disponivel no app compilado (.exe).",
        )

    asset_url = _pick_release_zip_asset_url(release_data)
    if not asset_url:
        return False, "Nenhum arquivo .zip foi encontrado na release mais recente."

    app_dir = os.path.dirname(sys.executable)
    exe_path = sys.executable
    if not os.path.isdir(app_dir) or not os.path.isfile(exe_path):
        return False, "Falha ao localizar o diretorio do app para atualizar."

    update_root = tempfile.mkdtemp(prefix="medstitch-update-")
    payload_dir = os.path.join(update_root, "payload")
    os.makedirs(payload_dir, exist_ok=True)
    zip_path = os.path.join(update_root, "update.zip")
    exe_name = os.path.basename(exe_path)

    progress = QProgressDialog("Baixando atualizacao...", "", 0, 1000, _main_window)
    progress.setWindowTitle("Atualizacao - Disponivel")
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setAutoClose(False)
    progress.setMinimumDuration(0)
    progress.setCancelButton(None)
    progress.show()

    def _on_progress(received: int, total: int) -> None:
        if total > 0:
            scaled = int((received / total) * 1000)
            progress.setRange(0, 1000)
            progress.setValue(min(1000, scaled))
            progress.setLabelText(
                f"Baixando atualizacao... {received // (1024 * 1024)}MB / {total // (1024 * 1024)}MB"
            )
        else:
            progress.setRange(0, 0)
            progress.setLabelText("Baixando atualizacao...")

        QApplication.processEvents()

    try:
        _download_file(asset_url, zip_path, progress_callback=_on_progress)

        progress.setRange(0, 1000)
        progress.setValue(1000)
        progress.setLabelText("Extraindo pacote de atualizacao...")
        QApplication.processEvents()

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(payload_dir)

        resolved_payload = _resolve_payload_dir(payload_dir, exe_name)
        if not os.path.isfile(os.path.join(resolved_payload, exe_name)):
            raise FileNotFoundError(
                f"Executavel '{exe_name}' nao encontrado no pacote de atualizacao."
            )
    except Exception as exc:
        progress.close()
        shutil.rmtree(update_root, ignore_errors=True)
        return False, f"Falha ao baixar/extrair a atualizacao: {exc}"

    progress.setLabelText("Atualizacao pronta. Reiniciando app...")
    QApplication.processEvents()
    progress.close()

    _run_external_updater(
        staged_dir=update_root,
        payload_dir=resolved_payload,
        app_dir=app_dir,
        exe_name=exe_name,
    )
    return True, "Atualizacao iniciada. Aplicando arquivos..."


def _check_for_updates(
    *, silent_if_latest: bool = False, auto_update: bool = False
) -> None:
    try:
        latest_release = _fetch_latest_release()
    except Exception:
        if silent_if_latest:
            return
        QMessageBox.warning(
            _main_window,
            "Atualizacao",
            "Nao foi possivel verificar atualizacoes agora.",
        )
        return

    latest_tag = str(latest_release.get("tag_name") or "").strip()
    latest_name = str(latest_release.get("name") or latest_tag or "ultima release")
    latest_url = str(latest_release.get("html_url") or GITHUB_RELEASES_URL)

    if _version_tuple(latest_tag) > _version_tuple(APP_VERSION):
        QMessageBox.information(
            _main_window,
            "Atualizacao disponivel",
            "Nova versao encontrada!\n\n"
            f"Atual: {APP_VERSION}\n"
            f"Disponivel: {latest_tag or latest_name}\n\n"
            "A pagina de releases sera aberta para baixar e instalar manualmente.",
        )
        try:
            webbrowser.open(latest_url, new=2)
        except Exception:
            QMessageBox.warning(
                _main_window,
                "Atualizacao",
                f"Nao foi possivel abrir o navegador.\nAcesse manualmente:\n{latest_url}",
            )
        return

    if silent_if_latest:
        return

    QMessageBox.information(
        _main_window,
        "Atualizacao",
        f"Voce ja esta na versao mais recente ({APP_VERSION}).",
    )


def _launch_process() -> None:
    if _process_thread is None:
        return

    if _process_thread.isRunning():
        return

    _main_window.processConsoleField.clear()

    input_path = (_main_window.inputField.text() or "").strip()
    output_path = input_path + OUTPUT_SUFFIX if input_path else ""

    _process_thread.configure(input_path=input_path, output_path=output_path)
    _process_thread.start()
