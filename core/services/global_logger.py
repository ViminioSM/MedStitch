import functools
import logging
import os
import sys
import traceback
from datetime import datetime

from ..utils.constants import LOG_REL_DIR


def _init_log_dir() -> str:
    """Ensure log directory exists, return path."""
    if not os.path.exists(LOG_REL_DIR):
        os.makedirs(LOG_REL_DIR, exist_ok=True)
    return LOG_REL_DIR


def _make_log_path(name: str, daily: bool = False) -> str:
    base = _init_log_dir()
    pid = os.getpid()
    if daily:
        timestamp = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(base, f"{name}_{timestamp}_pid{pid}.log")
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return os.path.join(base, f"{name}_{timestamp}_pid{pid}.log")


class GlobalLogger:
    """Centralized logging for all SmartStitch operations.

    Always writes to:
      %APPDATA%/SmartStitch/__logs__/smartstitch_YYYY-MM-DD_HHMMSS.log

    Debug-level function tracing enabled via SMARTSTITCH_DEBUG=1 env var.
    Errors and exceptions are ALWAYS logged regardless of debug setting.
    """

    _daily_log: str | None = None
    _configured: bool = False

    @classmethod
    def configure(cls) -> None:
        """Set up logging system. Call once at app startup."""
        if cls._configured:
            return

        _init_log_dir()
        cls._daily_log = _make_log_path("smartstitch", daily=True)

        # Root logger: always DEBUG to file, WARNING+ to console if attached
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        # File handler: everything, auto-flush
        fh = logging.FileHandler(cls._daily_log, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-7s] %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        fh.stream.reconfigure(line_buffering=True)  # auto-flush every write
        root.addHandler(fh)

        # Console handler: WARNING+ only (don't spam stdout)
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.WARNING)
        ch.setFormatter(logging.Formatter(
            "[%(levelname)s] %(name)s | %(message)s"
        ))
        root.addHandler(ch)

        # Silence noisy external loggers
        for noisy in ("PIL", "PIL.Image", "natsort", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        cls._configured = True
        logging.getLogger("smartstitch").info(
            "Logger initialized: %s", cls._daily_log
        )

    @classmethod
    def install_excepthook(cls) -> None:
        """Log all unhandled exceptions."""
        _original = sys.excepthook

        def _hook(exc_type, exc_value, exc_tb):
            if issubclass(exc_type, KeyboardInterrupt):
                _original(exc_type, exc_value, exc_tb)
                return

            tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
            logging.getLogger("smartstitch").critical(
                "Unhandled exception:\n%s", "".join(tb_lines)
            )
            _original(exc_type, exc_value, exc_tb)

        sys.excepthook = _hook

    @classmethod
    def log_startup(cls, **info) -> None:
        """Log application startup context."""
        log = logging.getLogger("smartstitch")
        log.info("=== SmartStitch Startup ===")
        log.info("Python: %s", sys.version)
        log.info("Platform: %s", sys.platform)
        log.info("Executable: %s", sys.executable)
        log.info("CWD: %s", os.getcwd())
        log.info("Args: %s", sys.argv)
        for key, value in info.items():
            log.info("  %s: %s", key, value)
        log.info("=== End Startup ===")

    @classmethod
    def log_shutdown(cls, elapsed: float | None = None) -> None:
        log = logging.getLogger("smartstitch")
        msg = "=== SmartStitch Shutdown ==="
        if elapsed is not None:
            msg += f" ({elapsed:.3f}s)"
        log.info(msg)
        logging.shutdown()  # flush all buffers


def logFunc(func=None, inclass=False):
    """Decorator: log function entry/exit. Errors always logged regardless of debug."""
    if func is None:
        return functools.partial(logFunc, inclass=inclass)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        logger = logging.getLogger("smartstitch")
        debug_enabled = os.getenv("SMARTSTITCH_DEBUG", "0").strip().lower() in {
            "1", "true", "yes", "on",
        }

        caller_class = ""
        if inclass and args:
            caller_class = type(args[0]).__name__

        if debug_enabled:
            args_repr = [repr(a) for a in (args[1:] if inclass else args)]
            kwargs_repr = [f"{k}={v!r}" for k, v in kwargs.items()]
            signature = ", ".join(args_repr + kwargs_repr)
            prefix = f"{caller_class}." if caller_class else ""
            logger.debug("→ %s%s(%s)", prefix, func.__name__, signature)

        try:
            result = func(*args, **kwargs)
            return result
        except Exception as e:
            prefix = f"{caller_class}." if caller_class else ""
            logger.exception(
                "Exception in %s%s: %s", prefix, func.__name__, e
            )
            raise

    return wrapper


# Auto-configure on import
_auto = os.getenv("SMARTSTITCH_LOG_AUTO", "1").strip()
if _auto.lower() in {"1", "true", "yes", "on"} and not GlobalLogger._configured:
    GlobalLogger.configure()
    GlobalLogger.install_excepthook()
