import os
import shutil
import sys
from pathlib import Path


def main() -> None:
    """Build a clean, single-folder GUI package using PyInstaller.

    This script keeps the repository tidy by:
    - Using a dedicated work directory for PyInstaller artifacts.
    - Cleaning that work directory after a successful build.
    - Leaving only ``dist/SmartStitch`` as the final application folder.
    """

    try:
        from PyInstaller.__main__ import run as pyinstaller_run
    except ImportError:  # pragma: no cover - defensive runtime check
        print("[Build] PyInstaller is not installed. Install it with 'pip install pyinstaller'.")
        sys.exit(1)

    project_root = Path(__file__).resolve().parent.parent
    os.chdir(project_root)

    gui_entry = project_root / "SmartStitchGUI.py"
    if not gui_entry.exists():
        print(f"[Build] Entry point not found: {gui_entry}")
        sys.exit(1)

    icon_path = project_root / "assets" / "SmartStitchLogo.ico"
    ui_path = project_root / "gui" / "layout.ui"

    dist_dir = project_root / "dist"
    app_name = "SmartStitch"
    app_dist_dir = dist_dir / app_name

    # Isolate PyInstaller's temporary artifacts under build/pyinstaller
    work_dir = project_root / "build" / "pyinstaller"
    work_dir.mkdir(parents=True, exist_ok=True)

    add_data_args: list[str] = []
    if icon_path.exists():
        add_data_args.extend(["--add-data", f"{icon_path};assets"])
    if ui_path.exists():
        add_data_args.extend(["--add-data", f"{ui_path};gui"])

    # Exclude large optional dependencies that may exist in the local Python env.
    # SmartStitch does not require these to run, but PyInstaller can still pick
    # them up via transitive hooks when they're installed.
    exclude_modules = [
        "torch",
        "torchvision",
        "cv2",
        "opencv_python",
        "scipy",
        "sklearn",
        "transformers",
        "tokenizers",
        "sentencepiece",
        "fugashi",
        "manga_ocr",
        "onnxruntime",
        "tensorflow",
        "jax",
        # Qt binding conflict: PySide6 is the only one we use
        "PyQt6",
        "PyQt6.sip",
        "PyQt5",
        "PyQt5.sip",
    ]

    # Hidden imports to avoid slow analysis and potential missing modules
    hidden_imports = [
        "PIL",
        "PIL.ImageQt",
        "numpy",
        "PySide6",
        "psd_tools",
        "natsort",
        "backports.tarfile",
    ]

    args: list[str] = [
        str(gui_entry),
        "--name",
        app_name,
        "--noconfirm",
        "--noconsole",
        "--clean",
        "--onedir",
        "--contents-directory",
        ".",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
        # Optimization: bytecode optimization (removes assertions + docstrings, ~10% size reduction)
        "--optimize",
        "2",
        # Optimization: Windows-only DLL exclusions
        "--exclude-module",
        "numpy.core._multiarray_umath_compat",
        "--exclude-module",
        "PIL.ImageTk",
    ]

    args.extend(add_data_args)

    # Add explicit hidden imports to speed up module analysis
    for module_name in hidden_imports:
        args.extend(["--hidden-import", module_name])

    for module_name in exclude_modules:
        args.extend(["--exclude-module", module_name])

    if icon_path.exists():
        args.extend(["--icon", str(icon_path)])

    print("[Build] Starting PyInstaller build...")
    print(f"[Build] Entry:   {gui_entry}")
    print(f"[Build] Dist:    {dist_dir}")
    print(f"[Build] Workdir: {work_dir}")
    print("[Build] Optimizations enabled:")
    print("[Build]   - Bytecode optimization (--optimize 2)")
    print("[Build]   - Explicit hidden imports for faster analysis")
    print("[Build]   - Windows DLL exclusions")

    try:
        pyinstaller_run(args)
    except SystemExit as exc:  # PyInstaller may call sys.exit
        code = int(getattr(exc, "code", 1) or 0)
        if code != 0:
            print(f"[Build] PyInstaller failed with exit code {code}.")
            sys.exit(code)

    # Clean up intermediate build artifacts
    if work_dir.exists():
        print(f"[Build] Removing temporary work directory: {work_dir}")
        shutil.rmtree(work_dir, ignore_errors=True)

    # Remove any stray .spec files in project root if present
    for spec in project_root.glob("*.spec"):
        try:
            print(f"[Build] Removing stray spec file: {spec}")
            spec.unlink()
        except OSError:
            pass

    print("[Build] Done.")
    if app_dist_dir.exists():
        print(f"[Build] Final application folder: {app_dist_dir}")
    else:
        print("[Build] Warning: expected dist folder not found. Check PyInstaller output.")


if __name__ == "__main__":
    main()
