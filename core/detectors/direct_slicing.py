"""Direct slicing detector for fixed-interval slicing."""
from PIL import Image as pil

from core.services.global_logger import logFunc


class DirectSlicingDetector:
    """Slices images at fixed intervals without detection logic."""

    @logFunc(inclass=True)
    def run(
        self,
        combined_img: pil.Image,
        split_height: int,
        **kwargs,
    ) -> list[int]:
        """Slice at fixed intervals of split_height.
        
        Args:
            combined_img: Combined vertical image to slice
            split_height: Fixed height for each slice
            
        Returns:
            List of y-coordinates for slice locations
        """
        last_row = combined_img.size[1]
        slice_locations = [0]
        row = split_height
        
        while row < last_row:
            slice_locations.append(row)
            row += split_height
        
        if slice_locations[-1] != last_row:
            slice_locations.append(last_row)
        
        return slice_locations
