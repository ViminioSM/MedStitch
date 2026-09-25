"""SmartStitch CLI entry point."""

import os
import sys
import traceback

# --- Early crash log ---
def _startup_crash_log() -> str | None:
    try:
        appdata = os.getenv("APPDATA") or os.path.expanduser("~")
        log_dir = os.path.join(appdata, "SmartStitch", "__logs__")
        os.makedirs(log_dir, exist_ok=True)
        from datetime import datetime
        return os.path.join(
            log_dir,
            f"crash_console_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.log"
        )
    except Exception:
        return None

_CRASH_LOG = _startup_crash_log()

import multiprocessing

from console.launcher import launch
from core.services.global_logger import GlobalLogger
from core.i18n import init as init_i18n

if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        GlobalLogger.configure()
        GlobalLogger.install_excepthook()
        init_i18n()
        GlobalLogger.log_startup(mode="console", args=sys.argv)
        raise SystemExit(launch())
    except Exception:
        if _CRASH_LOG:
            with open(_CRASH_LOG, "a", encoding="utf-8") as f:
                traceback.print_exc(file=f)
        raise
    finally:
        try:
            GlobalLogger.log_shutdown()
        except Exception:
            pass
