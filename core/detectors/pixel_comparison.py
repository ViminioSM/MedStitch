"""Pixel comparison detector for finding optimal slice locations."""
import numpy as np
from PIL import Image as pil

from core.services.global_logger import logFunc


class PixelComparisonDetector:
    """Detects slice locations by comparing neighboring pixel values and uniform color blocks."""

    @logFunc(inclass=True)
    def run(
        self,
        combined_img: pil.Image,
        split_height: int,
        *,
        scan_step: int = 5,
        ignorable_pixels: int = 0,
        sensitivity: int = 90,
        **kwargs,
    ) -> list[int]:
        """Find optimal slice locations using pixel comparison and solid color block detection.
        
        Detects slicing points based on:
        1. Low pixel difference (original behavior)
        2. SOLID uniform color blocks with ≥95% black (0-30) or white (225-255) pixels
           - Must have low variance (std dev < 10) to avoid gradients
        
        Args:
            combined_img: Combined vertical image to slice
            split_height: Target height for each slice
            scan_step: Pixels to move when searching for slice point
            ignorable_pixels: Edge pixels to ignore in comparison
            sensitivity: Detection sensitivity (0-100, higher = stricter)
            
        Returns:
            List of y-coordinates for slice locations
        """
        # Convert to grayscale numpy array
        img_array = np.array(combined_img.convert('L'))
        threshold = int(255 * (1 - (sensitivity / 100)))
        last_row = len(img_array)
        
        slice_locations = [0]
        row = split_height
        move_up = True
        
        while row < last_row:
            row_pixels = img_array[row]
            active_pixels = row_pixels[ignorable_pixels:len(row_pixels) - ignorable_pixels]
            
            # Check if row is SOLID uniform color block (95-100% black or white)
            # Must be solid color, not gradient - verified by low standard deviation
            if len(active_pixels) > 0:
                black_pixels = np.sum(active_pixels <= 30)  # Black threshold
                white_pixels = np.sum(active_pixels >= 225)  # White threshold
                total_pixels = len(active_pixels)
                
                black_ratio = black_pixels / total_pixels
                white_ratio = white_pixels / total_pixels
                
                # Check if it's a solid block (95%+ uniform) AND low variance (not gradient)
                is_solid_black = black_ratio >= 0.95
                is_solid_white = white_ratio >= 0.95
                
                if is_solid_black or is_solid_white:
                    # Verify it's truly solid by checking standard deviation
                    # Solid blocks have very low std dev (<10), gradients have high std dev
                    std_dev = np.std(active_pixels)
                    
                    # Only slice if it's a solid block (low variance)
                    if std_dev < 10:
                        slice_locations.append(row)
                        row += split_height
                        move_up = True
                        continue
            
            # Original pixel difference detection
            can_slice = True
            for index in range(
                ignorable_pixels + 1, len(row_pixels) - ignorable_pixels
            ):
                prev_pixel = int(row_pixels[index - 1])
                next_pixel = int(row_pixels[index])
                value_diff = next_pixel - prev_pixel
                if value_diff > threshold or value_diff < -threshold:
                    can_slice = False
                    break
            
            if can_slice:
                slice_locations.append(row)
                row += split_height
                move_up = True
                continue
            
            if row - slice_locations[-1] <= 0.4 * split_height:
                row = slice_locations[-1] + split_height
                move_up = False
            
            if move_up:
                row -= scan_step
                continue
            
            row += scan_step
        
        # Only add final slice if remaining height is significant (>50 pixels)
        # This prevents creating tiny images at the end
        remaining_height = last_row - slice_locations[-1]
        if remaining_height > 50:
            slice_locations.append(last_row)
        elif slice_locations[-1] != last_row:
            # Extend the last slice to include the remaining pixels
            slice_locations[-1] = last_row

        # Guard against tiny-edge cases: keep boundaries valid and strictly increasing.
        if not slice_locations or slice_locations[0] != 0:
            slice_locations.insert(0, 0)

        end_row = max(1, int(last_row))
        if slice_locations[-1] != end_row:
            slice_locations.append(end_row)

        normalized: list[int] = []
        for point in slice_locations:
            p = max(0, min(int(point), end_row))
            if not normalized or p > normalized[-1]:
                normalized.append(p)

        if len(normalized) < 2:
            normalized = [0, end_row]
        slice_locations = normalized
        
        return slice_locations
