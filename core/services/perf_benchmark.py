"""Performance benchmark collection for SmartStitch pipelines."""
import json
import os
import threading
from datetime import datetime
from time import perf_counter
from typing import Any

from ..utils.constants import LOG_REL_DIR


def is_benchmark_enabled() -> bool:
    """Return True when benchmark collection is enabled via environment variable."""
    value = os.getenv("SMARTSTITCH_BENCHMARK", os.getenv("MEDSTITCH_BENCHMARK", "1"))
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class PerfBenchmark:
    """Collect and export per-stage timing metrics as JSON."""

    def __init__(self, *, mode: str, enabled: bool | None = None, metadata: dict[str, Any] | None = None):
        self.enabled = is_benchmark_enabled() if enabled is None else bool(enabled)
        self.mode = mode
        self.started_at = datetime.utcnow().isoformat() + "Z"
        self._started_clock = perf_counter()
        self._lock = threading.Lock()
        self._directories: list[dict[str, Any]] = []
        self._metadata = metadata or {}

    def add_directory(
        self,
        *,
        input_path: str,
        output_path: str,
        image_count: int,
        retries: int,
        stage_seconds: dict[str, float],
        success: bool,
        error: str | None = None,
    ) -> None:
        if not self.enabled:
            return

        item = {
            "input_path": input_path,
            "output_path": output_path,
            "image_count": int(image_count),
            "retries": int(retries),
            "success": bool(success),
            "error": error,
            "stage_seconds": {k: round(float(v), 6) for k, v in stage_seconds.items()},
            "directory_total_seconds": round(float(sum(stage_seconds.values())), 6),
        }
        with self._lock:
            self._directories.append(item)

    def _build_payload(self, total_elapsed_s: float) -> dict[str, Any]:
        stage_totals: dict[str, float] = {}
        total_images = 0
        total_retries = 0
        failures = 0

        for directory in self._directories:
            total_images += int(directory.get("image_count", 0))
            total_retries += int(directory.get("retries", 0))
            if not directory.get("success", True):
                failures += 1
            for stage, seconds in directory.get("stage_seconds", {}).items():
                stage_totals[stage] = stage_totals.get(stage, 0.0) + float(seconds)

        return {
            "version": 1,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "total_elapsed_seconds": round(float(total_elapsed_s), 6),
            "directories_total": len(self._directories),
            "directories_failed": failures,
            "directories_success": len(self._directories) - failures,
            "total_images": total_images,
            "total_retries": total_retries,
            "images_per_second": round(total_images / total_elapsed_s, 6) if total_elapsed_s > 0 else 0.0,
            "stage_totals_seconds": {k: round(v, 6) for k, v in stage_totals.items()},
            "metadata": self._metadata,
            "directories": self._directories,
        }

    def write_json(self, *, file_prefix: str = "benchmark", total_elapsed_s: float | None = None) -> str | None:
        if not self.enabled:
            return None

        elapsed = float(total_elapsed_s) if total_elapsed_s is not None else perf_counter() - self._started_clock
        payload = self._build_payload(elapsed)

        os.makedirs(LOG_REL_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"{file_prefix}-{self.mode}-{timestamp}.json"
        file_path = os.path.join(LOG_REL_DIR, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=True)

        return file_path
