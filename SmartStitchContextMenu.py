import argparse
import multiprocessing
import os

from core.services import SettingsHandler
from core.utils.constants import OUTPUT_SUFFIX
from gui.process import GuiStitchProcess


def _apply_preset(settings: SettingsHandler, preset: str) -> None:
    preset = (preset or "").strip().lower()
    if preset not in {"type", "redraw"}:
        raise ValueError("preset must be 'type' or 'redraw'")

    # Shared settings
    settings.save("lossy_quality", 100)
    settings.save("enforce_type", 2)
    settings.save("enforce_width", 800)

    if preset == "type":
        settings.save("output_type", ".webp")
        settings.save("split_height", 5000)
        settings.save("detector_type", 0)
        settings.save("postprocess_args", "-i [stitched] -o [processed] -n 3 -s 1 -f webp")
    else:
        settings.save("output_type", ".jpg")
        settings.save("split_height", 15000)
        settings.save("detector_type", 1)
        settings.save("sensitivity", 100)
        settings.save("scan_step", 10)
        settings.save("ignorable_pixels", 0)
        settings.save("postprocess_args", "-i [stitched] -o [processed] -n 3 -s 1 -f jpg")


def launch() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--preset",
        required=True,
        choices=["type", "redraw"],
        help="Which preset to use",
    )
    parser.add_argument(
        "--input",
        required=True,
        dest="input_path",
        help="Folder path to process",
    )
    args = parser.parse_args()

    input_path = os.path.abspath(args.input_path)
    if not os.path.isdir(input_path):
        raise FileNotFoundError(f"Input folder not found: {input_path}")

    settings = SettingsHandler()
    _apply_preset(settings, args.preset)

    output_path = input_path + OUTPUT_SUFFIX

    process = GuiStitchProcess()
    process.run_with_error_msgs(
        input_path=input_path,
        output_path=output_path,
        status_func=lambda pct, msg: print(f"[{pct}%] {msg}"),
        console_func=print,
    )
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(launch())
