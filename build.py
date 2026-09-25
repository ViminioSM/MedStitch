#!/usr/bin/env python3
"""SmartStitch unified build script.

Usage:
    python build.py              # Full build (Rust + GUI)
    python build.py --no-rust    # Skip Rust, PyInstaller only
    python build.py --no-exe     # Rust only, skip PyInstaller
    python build.py --release    # Full build + zip for distribution
    python build.py --clean      # Clean all build artifacts
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# ── Colors (Windows 10+ support via ANSI) ──────────────────────────────────
os.system("")  # enable ANSI escapes in Windows terminal

C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_CYAN = "\033[36m"
C_WHITE = "\033[37m"

OK = f"{C_GREEN}{C_BOLD}[OK]{C_RESET}"
FAIL = f"{C_RED}{C_BOLD}[FAIL]{C_RESET}"
ARROW = f"{C_CYAN}->{C_RESET}"
GEAR = f"{C_YELLOW}[*]{C_RESET}"

PROJECT_ROOT = Path(__file__).resolve().parent
RUST_DIR = PROJECT_ROOT / "native"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
APP_NAME = "SmartStitch"


def status(msg: str) -> None:
    print(f"  {GEAR} {msg}")


def success(msg: str) -> None:
    print(f"  {OK} {C_GREEN}{msg}{C_RESET}")


def warn(msg: str) -> None:
    print(f"  {C_YELLOW}{C_BOLD}!{C_RESET} {C_YELLOW}{msg}{C_RESET}")


def error(msg: str) -> None:
    print(f"  {FAIL} {C_RED}{msg}{C_RESET}")


def header(title: str) -> None:
    print(f"\n{C_BOLD}{C_BLUE}{'=' * 60}{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}  {title}{C_RESET}")
    print(f"{C_BOLD}{C_BLUE}{'=' * 60}{C_RESET}\n")


def separator() -> None:
    print(f"{C_DIM}{'-' * 60}{C_RESET}")


def run_cmd(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 300, silent: bool = False) -> bool:
    """Run a command. Returns True on success. If silent, suppress output."""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if not silent:
                stripped = line.rstrip()
                if stripped and any(kw in stripped.upper() for kw in ("ERROR", "FAIL", "error:", "Error:")):
                    try:
                        print(f"    {C_DIM}{stripped}{C_RESET}")
                    except Exception:
                        pass
        proc.wait(timeout=timeout)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        error(f"Timed out after {timeout}s: {' '.join(cmd)}")
        return False
    except Exception as e:
        error(f"Command failed: {e}")
        return False


def check_tool(name: str, cmd: list[str]) -> Optional[str]:
    """Check if a tool is available. Returns version string or None."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            version = result.stdout.strip().split("\n")[0]
            return version
        return None
    except Exception:
        return None


# ── Build Steps ────────────────────────────────────────────────────────────

def step_check_environment() -> dict:
    """Verify all required tools are installed."""
    info = {}

    py = check_tool("Python", [sys.executable, "--version"])
    if py:
        info["python"] = py
        success(f"Python: {py}")
    else:
        error("Python not found")
        sys.exit(1)

    pip = check_tool("pip", [sys.executable, "-m", "pip", "--version"])
    if pip:
        info["pip"] = pip.split()[1]
    else:
        warn("pip not found")

    maturin = check_tool("maturin", [sys.executable, "-m", "maturin", "--version"])
    if maturin:
        info["maturin"] = maturin.strip()
        success(f"maturin: {maturin.strip()}")
    else:
        warn("maturin not installed (Rust build disabled)")
        info["maturin"] = None

    rust = check_tool("rustc", ["rustc", "--version"])
    if rust:
        info["rustc"] = rust.split(" ")[1] if " " in rust else rust
        success(f"rustc: {info['rustc']}")
    else:
        if maturin:
            warn("rustc not found but maturin is installed")

    pyi = check_tool("PyInstaller", [sys.executable, "-c", "import PyInstaller; print(PyInstaller.__version__)"])
    if pyi:
        info["pyinstaller"] = pyi.strip()
        success(f"PyInstaller: {pyi.strip()}")
    else:
        warn("PyInstaller not installed (pip install pyinstaller)")
        info["pyinstaller"] = None

    return info


def step_rust(info: dict) -> bool:
    """Build Rust native extension via maturin."""
    if not info.get("maturin"):
        warn("Skipping Rust build (maturin not available)")
        return True

    if not RUST_DIR.exists():
        warn(f"Skipping Rust build ({RUST_DIR} not found)")
        return True

    status(f"Building Rust extension ({RUST_DIR})...")
    ok = run_cmd(
        [sys.executable, "-m", "maturin", "build", "--release"],
        cwd=RUST_DIR,
        timeout=300,
        silent=True,
    )
    if not ok:
        error("Rust build failed")
        return False

    # Install the wheel
    wheels = list((RUST_DIR / "target" / "wheels").glob("*.whl"))
    if wheels:
        latest = max(wheels, key=lambda p: p.stat().st_mtime)
        status(f"Installing {latest.name}...")
        ok = run_cmd(
            [sys.executable, "-m", "pip", "install", "--force-reinstall", str(latest), "--quiet"],
            timeout=60,
            silent=True,
        )
        if ok:
            success(f"Rust extension installed ({latest.name})")
            return True
        error("Failed to install Rust wheel")
        return False

    error("No Rust wheel found")
    return False


def step_kill_existing() -> None:
    """Kill running SmartStitch.exe to allow clean rebuild."""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", f"{APP_NAME}.exe"],
            capture_output=True, text=True,
            timeout=10,
        )
        if result.returncode == 0:
            status("Terminated running SmartStitch.exe")
    except Exception:
        pass


def step_clean_dist() -> bool:
    """Remove old dist folder."""
    app_dir = DIST_DIR / APP_NAME
    if app_dir.exists():
        status(f"Removing old build: {app_dir}")
        try:
            shutil.rmtree(app_dir)
        except PermissionError:
            error(f"Permission denied: {app_dir} (close {APP_NAME}.exe first)")
            return False
    return True


def step_pyinstaller(info: dict) -> bool:
    """Build the PyInstaller executable."""
    if not info.get("pyinstaller"):
        error("PyInstaller not installed. Run: pip install pyinstaller")
        return False

    entry = PROJECT_ROOT / "SmartStitchGUI.py"
    if not entry.exists():
        error(f"Entry point not found: {entry}")
        return False

    status("Building GUI executable...")

    # Reuse the existing build script's logic
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    try:
        from scripts.build import main as pyinstaller_build
        pyinstaller_build()
        return True
    except SystemExit as e:
        if e.code != 0:
            error(f"PyInstaller build failed (exit {e.code})")
            return False
        return True
    except Exception as e:
        error(f"PyInstaller build error: {e}")
        return False


def step_verify() -> bool:
    """Verify the build output exists."""
    exe = DIST_DIR / APP_NAME / f"{APP_NAME}.exe"
    if exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        success(f"Build verified: {exe}")
        print(f"      Size: {size_mb:.1f} MB")
        return True
    error(f"Build output not found: {exe}")
    return False


def step_zip() -> Optional[Path]:
    """Create a zip archive for distribution."""
    app_dir = DIST_DIR / APP_NAME
    if not app_dir.exists():
        error("Nothing to zip (build first)")
        return None

    status("Creating distribution zip...")
    zip_path = DIST_DIR / f"{APP_NAME}.zip"
    shutil.make_archive(
        str(DIST_DIR / APP_NAME),
        "zip",
        root_dir=DIST_DIR,
        base_dir=APP_NAME,
    )
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    success(f"Release: {zip_path} ({size_mb:.1f} MB)")
    return zip_path


def step_clean_all() -> None:
    """Remove all build artifacts."""
    for path in [DIST_DIR, BUILD_DIR]:
        if path.exists():
            status(f"Removing {path}")
            shutil.rmtree(path, ignore_errors=True)
    for spec in PROJECT_ROOT.glob("*.spec"):
        spec.unlink(missing_ok=True)
    success("All build artifacts removed")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SmartStitch Unified Build Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python build.py                 Full build (Rust + EXE)
  python build.py --no-rust       PyInstaller only
  python build.py --no-exe        Rust extension only
  python build.py --release       Full build + distribution zip
  python build.py --clean         Remove all build artifacts
        """,
    )
    parser.add_argument("--no-rust", action="store_true", help="Skip Rust native extension build")
    parser.add_argument("--no-exe", action="store_true", help="Skip PyInstaller EXE build")
    parser.add_argument("--release", action="store_true", help="Create distribution zip after build")
    parser.add_argument("--clean", action="store_true", help="Remove all build artifacts and exit")
    parser.add_argument("--kill", action="store_true", help="Kill running SmartStitch.exe before building")

    args = parser.parse_args()

    if args.clean:
        step_kill_existing()
        step_clean_all()
        return

    header("SmartStitch Build Tool")
    t_start = time.time()

    # 1. Environment
    info = step_check_environment()
    separator()

    # 2. Kill existing
    if args.kill:
        step_kill_existing()

    # 3. Clean dist
    if not args.no_exe:
        if not step_clean_dist():
            sys.exit(1)

    # 4. Rust
    if not args.no_rust and info.get("maturin"):
        if not step_rust(info):
            sys.exit(1)
        separator()

    # 5. PyInstaller
    if not args.no_exe:
        if not step_pyinstaller(info):
            sys.exit(1)
        separator()

    # 6. Verify
    if not args.no_exe:
        if not step_verify():
            sys.exit(1)

    # 7. Zip
    if args.release and not args.no_exe:
        step_zip()
        separator()

    elapsed = time.time() - t_start
    header(f"Build complete in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
