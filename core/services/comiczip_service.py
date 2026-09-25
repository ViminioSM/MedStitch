"""Internal ComicZip service (no external .py invocation required)."""

import os
import zipfile
from typing import Callable

from core.services.global_logger import logFunc

DEFAULT_OUTPUT = "output.zip"

# Already-compressed outputs: DEFLATE burns CPU for ~0% gain. STORED extracts
# byte-identical files, so this is a pure speed win with zero quality impact.
_ALREADY_COMPRESSED_EXTS = frozenset(
    {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".avif"}
)


def _resolve_compression(
    files: list[str], requested: int | None,
) -> int:
    """Pick ZIP_STORED vs ZIP_DEFLATED without changing extracted bytes."""
    override = (os.getenv("SMARTSTITCH_ZIP_COMPRESSION") or "").strip().lower()
    if override in ("stored", "store", "0"):
        return zipfile.ZIP_STORED
    if override in ("deflated", "deflate", "8"):
        return zipfile.ZIP_DEFLATED
    if requested is not None:
        return requested
    # auto (default): STORED when every file is already compressed.
    if files and all(
        os.path.splitext(f)[1].lower() in _ALREADY_COMPRESSED_EXTS for f in files
    ):
        if (os.getenv("SMARTSTITCH_ZIP_STORED") or "").strip().lower() not in {
            "0", "false", "no", "off"
        }:
            return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


class ComicZipService:
    """Creates zip archives from stitched output folders."""

    @staticmethod
    def _resolve_output_path(input_root: str, output: str) -> str:
        if os.path.isdir(output):
            base_name = os.path.basename(os.path.normpath(input_root))
            if not base_name:
                base_name = DEFAULT_OUTPUT
            return os.path.join(output, f"{base_name}.zip")
        return output

    @logFunc(inclass=True)
    def compress_input(
        self,
        input_root: str,
        output: str,
        *,
        console_func: Callable[[str], None] = print,
        compression: int | None = None,
    ) -> str:
        """Compress a file or all direct files from a directory into a zip archive."""
        if not input_root:
            raise ValueError("ComicZip input path is required.")

        if not os.path.exists(input_root):
            raise FileNotFoundError(f"ComicZip input not found: {input_root}")

        if os.path.isdir(input_root):
            files = [entry.path for entry in os.scandir(input_root) if entry.is_file()]
        else:
            files = [input_root]

        if not files:
            raise RuntimeError(f"No files found to zip in: {input_root}")

        zip_path = self._resolve_output_path(input_root, output)
        # Always create the parent of the zip file — never makedirs(output) when
        # output is a file path (that would create a directory named like the zip).
        zip_parent = os.path.dirname(os.path.abspath(zip_path))
        if zip_parent:
            os.makedirs(zip_parent, exist_ok=True)

        console_func(f"Creating ComicZip archive: {zip_path}\n")
        effective_compression = _resolve_compression(files, compression)
        with zipfile.ZipFile(zip_path, mode="w") as zf:
            for file_path in files:
                zf.write(
                    file_path, os.path.basename(file_path), compress_type=effective_compression
                )

        console_func("ComicZip finished successfully!\n")
        return zip_path
