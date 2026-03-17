"""Shared image utility functions for SmartStitch."""

_MAX_PIL_IMAGE_DIMENSION = 65500
_SENSITIVITY_RETRY_FACTOR = 0.9
_MAX_SENSITIVITY_RETRIES = 3


def is_dimension_error(exc: Exception) -> bool:
    """Check if exception is related to image dimension limits."""
    msg = str(exc).lower()
    return (
        "maximum supported image dimension" in msg
        or "broken data stream" in msg
        or "encoder error" in msg
        or "output image exceeds" in msg
    )


def ensure_max_slice_segment(
    slice_points: list[int],
    *,
    combined_height: int,
    max_segment: int = _MAX_PIL_IMAGE_DIMENSION,
) -> list[int]:
    """Ensure no slice segment exceeds max_segment height.
    
    Args:
        slice_points: List of y-coordinates for slice locations
        combined_height: Total height of the combined image
        max_segment: Maximum allowed segment height
        
    Returns:
        Modified list of slice points with no segment exceeding max_segment
    """
    if not slice_points:
        slice_points = [0, combined_height]

    points = sorted(set(int(p) for p in slice_points if p is not None))
    if not points or points[0] != 0:
        points.insert(0, 0)
    if points[-1] != combined_height:
        points.append(combined_height)

    enforced: list[int] = [points[0]]
    for p in points[1:]:
        last = enforced[-1]
        while p - last > max_segment:
            last = last + max_segment
            enforced.append(last)
        enforced.append(p)

    return enforced


def close_images_safely(*image_lists) -> None:
    """Safely close PIL Image objects, ignoring any errors.
    
    Args:
        *image_lists: Any number of images or lists of images to close
    """
    for item in image_lists:
        if item is None:
            continue
        if isinstance(item, list):
            for im in item:
                try:
                    im.close()
                except Exception:
                    pass
        else:
            try:
                item.close()
            except Exception:
                pass
