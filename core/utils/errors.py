"""Custom exceptions for SmartStitch application."""


class SmartStitchError(Exception):
    """Base exception for all SmartStitch errors."""

    pass


class DirectoryException(SmartStitchError):
    """Raised when there's an issue with directory operations."""

    pass
