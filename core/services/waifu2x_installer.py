"""Waifu2X download/install shared by GUI and console."""

from __future__ import annotations

import os
import shutil
import tempfile
import urllib.request
import zipfile
from typing import Callable
from urllib.parse import urlparse

WAIFU_ZIP_URL = (
    "https://github.com/ViminioSM/MedStitch/releases/download/waifu2x/Waifu2X.zip"
)
WAIFU_INSTALL_DIR = "C:/Manhwa/Waifu2X"
WAIFU_EXE_NAME = "waifu2x-ncnn-vulkan.exe"
WAIFU_EXE_PATH = os.path.join(WAIFU_INSTALL_DIR, WAIFU_EXE_NAME)
WAIFU_ARGS_JPG = "-i [stitched] -o [processed] -n 3 -s 1 -f jpg"
WAIFU_ARGS_WEBP = "-i [stitched] -o [processed] -n 3 -s 1 -f webp"
WAIFU_ARGS_PNG = "-i [stitched] -o [processed] -n 3 -s 1 -f png"
WAIFU_ARGS_AVIF = "-i [stitched] -o [processed] -n 3 -s 1 -f png"

_SAFE_UPDATE_HOSTS = (
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "githubusercontent.com",
)

ProgressCallback = Callable[[int, int], None]
ConsoleFunc = Callable[[str], None]


def assert_safe_http_url(url: str, *, allowed_hosts: tuple[str, ...] | None = None) -> str:
    """Validate URL scheme and host before network calls."""
    hosts = allowed_hosts or _SAFE_UPDATE_HOSTS
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme or '<empty>'}")
    if not host:
        raise ValueError("URL host is missing.")
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in hosts):
        raise ValueError(f"Host is not allowed for download: {host}")
    return parsed.geturl()


def is_waifu2x_installed(exe_path: str | None = None) -> bool:
    path = exe_path or WAIFU_EXE_PATH
    return os.path.isfile(path)


def download_file(
    url: str,
    target_path: str,
    *,
    progress_callback: ProgressCallback | None = None,
    user_agent: str = "MedStitch",
    timeout: int = 120,
) -> None:
    """Download *url* to *target_path* with optional progress callback(received, total)."""
    safe_url = assert_safe_http_url(url)
    request = urllib.request.Request(
        safe_url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": user_agent,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
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


def download_and_extract_waifu2x(
    *,
    repair: bool = False,
    install_dir: str | None = None,
    exe_path: str | None = None,
    zip_url: str | None = None,
    progress_callback: ProgressCallback | None = None,
    console_func: ConsoleFunc | None = None,
) -> str:
    """Download and extract Waifu2X. Returns absolute path to the executable.

    Same behavior as the GUI install/repair buttons.
    """
    target_dir = install_dir or WAIFU_INSTALL_DIR
    target_exe = exe_path or os.path.join(target_dir, WAIFU_EXE_NAME)
    url = zip_url or WAIFU_ZIP_URL
    log = console_func or (lambda _msg: None)

    if repair and os.path.isdir(target_dir):
        log(f"Removing existing Waifu2X install at: {target_dir}\n")
        shutil.rmtree(target_dir, ignore_errors=True)

    os.makedirs(target_dir, exist_ok=True)

    tmp_dir = tempfile.mkdtemp(prefix="medstitch-waifu2x-")
    zip_path = os.path.join(tmp_dir, "Waifu2X.zip")
    try:
        log(f"Downloading Waifu2X from {url} ...\n")
        download_file(url, zip_path, progress_callback=progress_callback)
        log(f"Extracting Waifu2X to {target_dir} ...\n")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(target_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Zip layouts vary: exe may land in a nested folder
    if not os.path.isfile(target_exe):
        found = _find_exe_under(target_dir, WAIFU_EXE_NAME)
        if found:
            target_exe = found

    if not os.path.isfile(target_exe):
        raise FileNotFoundError(f"Waifu2X exe not found after install at: {target_exe}")

    log(f"Waifu2X ready: {target_exe}\n")
    return os.path.abspath(target_exe)


def ensure_waifu2x_installed(
    *,
    exe_path: str | None = None,
    repair: bool = False,
    progress_callback: ProgressCallback | None = None,
    console_func: ConsoleFunc | None = None,
) -> str:
    """Return path to Waifu2X exe, downloading it if missing (or when repair=True)."""
    path = exe_path or WAIFU_EXE_PATH
    log = console_func or (lambda _msg: None)

    if not repair and is_waifu2x_installed(path):
        return os.path.abspath(path)

    # If a custom absolute path was requested and is missing, still install to default
    # then prefer default unless user path becomes available.
    if repair:
        log("Repairing Waifu2X installation...\n")
    else:
        log(f"Waifu2X not found at '{path}'. Downloading (same as GUI install)...\n")

    installed = download_and_extract_waifu2x(
        repair=repair,
        progress_callback=progress_callback,
        console_func=console_func,
    )
    return installed


def _find_exe_under(root: str, exe_name: str) -> str | None:
    direct = os.path.join(root, exe_name)
    if os.path.isfile(direct):
        return direct
    for dirpath, _, files in os.walk(root):
        if exe_name in files:
            return os.path.join(dirpath, exe_name)
    return None
