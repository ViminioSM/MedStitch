"""Detector selection based on detection type."""
from typing import Union

from core.utils.constants import DETECTION_TYPE

from ..services import logFunc
from .direct_slicing import DirectSlicingDetector
from .pixel_comparison import PixelComparisonDetector

Detector = Union[DirectSlicingDetector, PixelComparisonDetector]


@logFunc()
def select_detector(detection_type: str | int | DETECTION_TYPE) -> Detector:
    """Select and return the appropriate detector based on detection type.
    
    Args:
        detection_type: Detection type as string ('none', 'pixel'), 
                       int enum value, or DETECTION_TYPE enum
                       
    Returns:
        Detector instance (DirectSlicingDetector or PixelComparisonDetector)
        
    Raises:
        ValueError: If detection_type is not recognized
    """
    if detection_type in ("none", DETECTION_TYPE.NO_DETECTION, DETECTION_TYPE.NO_DETECTION.value):
        return DirectSlicingDetector()
    
    if detection_type in ("pixel", DETECTION_TYPE.PIXEL_COMPARISON, DETECTION_TYPE.PIXEL_COMPARISON.value):
        return PixelComparisonDetector()
    
    raise ValueError(f"Invalid detection type: {detection_type}")
