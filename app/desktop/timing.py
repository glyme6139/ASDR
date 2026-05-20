"""
Lightweight timing helpers for optional runtime profiling.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, asdict
import logging
import threading
import time
from typing import Callable, Iterator, Optional


@dataclass(frozen=True)
class TimingConfig:
    enabled: bool = True
    sample_count: int = 100


@dataclass(frozen=True)
class TimingReport:
    source: str
    stage: str
    samples: int
    mean_ms: float
    min_ms: float
    max_ms: float
    timestamp: float


class TimingProfiler:
    def __init__(self, config: Optional[TimingConfig] = None, logger: Optional[logging.Logger] = None,
                 prefix: str = "Timing", report_handler: Optional[Callable[[dict], None]] = None):
        self._config = config or TimingConfig()
        self._logger = logger or logging.getLogger(__name__)
        self._prefix = prefix
        self._report_handler = report_handler
        self._lock = threading.RLock()
        self._stats: dict[str, list[float]] = {}
        self._sample_count = max(1, int(self._config.sample_count))

    @property
    def enabled(self) -> bool:
        return self._config.enabled and self._sample_count > 0

    @property
    def sample_count(self) -> int:
        return self._sample_count

    def set_sample_count(self, n: int) -> None:
        with self._lock:
            self._sample_count = max(1, int(n))

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return

        start = time.perf_counter_ns()
        try:
            yield
        finally:
            self.record(name, time.perf_counter_ns() - start)

    def record(self, name: str, elapsed_ns: int) -> None:
        if not self.enabled:
            return

        threshold = self.sample_count
        with self._lock:
            count, total_ns, min_ns, max_ns = self._stats.get(name, [0, 0.0, float("inf"), 0.0])
            count += 1
            total_ns += float(elapsed_ns)
            min_ns = min(min_ns, float(elapsed_ns))
            max_ns = max(max_ns, float(elapsed_ns))

            if count < threshold:
                self._stats[name] = [count, total_ns, min_ns, max_ns]
                return

            avg_ns = total_ns / count
            self._stats[name] = [0, 0.0, float("inf"), 0.0]
            report = TimingReport(
                source=self._prefix,
                stage=name,
                samples=count,
                mean_ms=avg_ns / 1e6,
                min_ms=min_ns / 1e6,
                max_ms=max_ns / 1e6,
                timestamp=time.time(),
            )

        self._emit_report(report)

    def _emit_report(self, report: TimingReport) -> None:
        payload = asdict(report)
        if self._report_handler is not None:
            try:
                self._report_handler(payload)
                return
            except Exception:
                self._logger.exception("Failed to deliver timing report")
                return

        self._logger.info(
            "%s %s avg over %d samples: mean=%.3f ms min=%.3f ms max=%.3f ms",
            report.source,
            report.stage,
            report.samples,
            report.mean_ms,
            report.min_ms,
            report.max_ms,
        )


def profiler_from_config(
    config: Optional[TimingConfig],
    logger: Optional[logging.Logger] = None,
    prefix: str = "Timing",
    report_handler: Optional[Callable[[dict], None]] = None,
) -> TimingProfiler:
    return TimingProfiler(config=config, logger=logger, prefix=prefix, report_handler=report_handler)