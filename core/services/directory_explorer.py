"""Directory exploration service for finding image directories."""
import os

from natsort import natsorted

from ..models import WorkDirectory
from ..utils.constants import OUTPUT_SUFFIX, POSTPROCESS_SUFFIX, SUPPORTED_IMG_TYPES
from ..utils.errors import DirectoryException
from .global_logger import logFunc


class DirectoryExplorer:
    """Explores directories to find image files for processing."""

    def run(
        self,
        input: str,
        *,
        output: str | None = None,
        postprocess: str | None = None,
    ) -> list[WorkDirectory]:
        """Find all work directories containing images.
        
        Args:
            input: Input directory path
            output: Optional output directory path
            postprocess: Optional postprocess directory path
            
        Returns:
            List of WorkDirectory objects with image files
        """
        main_directory = self._get_main_directory(input, output, postprocess)
        return self._explore_directories(main_directory)

    @logFunc(inclass=True)
    def _get_main_directory(
        self,
        input_path: str,
        output_path: str | None,
        postprocess_path: str | None,
    ) -> WorkDirectory:
        """Create the main WorkDirectory from input paths."""
        if not input_path:
            raise DirectoryException("Missing Input Directory")
        
        abs_input = os.path.abspath(input_path)
        abs_output = output_path or (abs_input + OUTPUT_SUFFIX)
        abs_postprocess = postprocess_path or (abs_input + POSTPROCESS_SUFFIX)
        
        return WorkDirectory(abs_input, abs_output, abs_postprocess)

    @logFunc(inclass=True)
    def _explore_directories(self, main_dir: WorkDirectory) -> list[WorkDirectory]:
        """Recursively find all directories containing supported images."""
        work_directories: list[WorkDirectory] = []
        
        for dir_root, _, files in os.walk(main_dir.input_path, topdown=True):
            img_files = [
                f for f in files
                if f.lower().endswith(SUPPORTED_IMG_TYPES)
            ]
            
            if not img_files:
                continue
                
            img_files = natsorted(img_files)
            rel_root = os.path.relpath(dir_root, main_dir.input_path)
            
            directory = WorkDirectory(
                input=dir_root,
                output=os.path.join(main_dir.output_path, rel_root),
                postprocess=os.path.join(main_dir.postprocess_path, rel_root),
            )
            directory.input_files = img_files
            work_directories.append(directory)
        
        if not work_directories:
            raise DirectoryException("No valid work directories were found!")
        
        return work_directories
