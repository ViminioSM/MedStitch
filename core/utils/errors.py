"""Custom exceptions for SmartStitch application."""


class SmartStitchError(Exception):
    """Base exception for all SmartStitch errors."""
    pass


class DirectoryException(SmartStitchError):
    """Raised when there's an issue with directory operations."""
    pass


class ProfileException(SmartStitchError):
    """Raised when there's an issue with profile operations."""
    pass


class ImageProcessingError(SmartStitchError):
    """Raised when image processing fails."""
    pass


class WatermarkError(SmartStitchError):
    """Raised when watermark application fails."""
    pass
