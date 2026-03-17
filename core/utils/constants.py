import os
from enum import IntEnum

# Static Variables
_APPDATA_ROOT = os.getenv("APPDATA") or os.path.expanduser("~")
_APP_ROOT = os.path.join(_APPDATA_ROOT, "SmartStitch")

LOG_REL_DIR = os.path.join(_APP_ROOT, "__logs__")
SETTINGS_REL_DIR = os.path.join(_APP_ROOT, "__settings__")
OUTPUT_SUFFIX = ' [stitched]'
POSTPROCESS_SUFFIX = ' [processed]'
SUPPORTED_IMG_TYPES = (
    '.png',
    '.webp',
    '.jpg',
    '.jpeg',
    '.jfif',
    '.bmp',
    '.tiff',
    '.tga',
    '.psd',
    '.psb',
)

PHOTOSHOP_FILE_TYPES = (
    ".psd",
    ".psb"
)

# Static Enums
class WIDTH_ENFORCEMENT(IntEnum):
    NONE = 0
    AUTOMATIC = 1
    MANUAL = 2


class DETECTION_TYPE(IntEnum):
    NO_DETECTION = 0
    PIXEL_COMPARISON = 1


class WATERMARK_FULLPAGE_POSITION(IntEnum):
    TOP = 0
    CENTER = 1
    BOTTOM = 2


class WATERMARK_FULLPAGE_FREQUENCY(IntEnum):
    ONCE_PER_PAGE = 0
    ALL_BLOCKS = 1
    ALTERNATING = 2


class WATERMARK_OVERLAY_POSITION(IntEnum):
    AUTO = 0
    TOP_LEFT = 1
    TOP_RIGHT = 2
    BOTTOM_LEFT = 3
    BOTTOM_RIGHT = 4
    CENTER = 5


class WATERMARK_FULLPAGE_BLOCK_STRATEGY(IntEnum):
    FIRST = 0
    BEST = 1
    RANDOM = 2
