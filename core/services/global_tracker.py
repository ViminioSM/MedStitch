"""
DEPRECATED: This module is not in use and will be removed in a future version.

This was an experimental Observer Pattern Tracker that was never implemented
into either the GUI or the console application.
"""
import warnings
from collections.abc import Callable
from typing import Any, cast

from ..utils.funcs import get_classname_stack, get_funcname_stack, print_tracking
from .global_logger import logFunc

warnings.warn(
    "GlobalTracker is deprecated and will be removed in a future version.",
    DeprecationWarning,
    stacklevel=2,
)


class GlobalTracker:
    # Array of functions subscribed on this tracker
    subscribers: list[Callable[[float, str], Any]] = [
        cast(Callable[[float, str], Any], print_tracking)
    ]
    # List of function that are being tracked/observed
    tracking_dict = {}
    process_count = 1
    progress_track = 0
    total_progress = 0

    @classmethod
    @logFunc(inclass=True)
    def reset(cls, process_count: int = 1):
        cls.process_count = process_count
        cls.progress_track = 0
        cls.update_total()

    @classmethod
    @logFunc(inclass=True)
    def add_subscriber(cls, subscriber_func: Callable[[float, str], Any]) -> None:
        cls.subscribers.append(subscriber_func)
        cls.subscribers = list(set(cls.subscribers))
        cls.update_total()

    @classmethod
    def add_tracking(cls, func_name: str, value: float):
        class_name = get_classname_stack(2)
        if class_name:
            func_name = class_name + '.' + func_name
        cls.tracking_dict[func_name] = value
        cls.update_total()

    @classmethod
    def remove_tracking(cls, func_name: str, value: float):
        class_name = get_classname_stack(2)
        if class_name:
            func_name = class_name + '.' + func_name
        cls.tracking_dict.pop(func_name, None)
        cls.update_total()

    @classmethod
    def update_total(cls):
        cls.total_progress = 0
        for value in cls.tracking_dict.values():
            cls.total_progress += value

    # Update & Message funcs is not logged so it does not spam the log file.
    @classmethod
    def update(cls, message: str | None = None, fraction: float = 1) -> None:
        class_name = get_classname_stack(2)
        func_name = get_funcname_stack(2)
        if class_name:
            func_name = class_name + '.' + func_name
        tracking_details = cls.tracking_dict.get(func_name, None)
        if tracking_details:
            value = (tracking_details * fraction) / cls.process_count
            cls.progress_track += value
            percentage = float(cls.progress_track / cls.total_progress) * 100
            if not message:
                message = func_name + ' ran successfully!'
            for subscriber in cls.subscribers:
                subscriber(percentage, message)
