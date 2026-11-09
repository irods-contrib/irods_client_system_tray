"""Session-only history of aggregate upload progress for the Throughput chart."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


MIB = 1024 * 1024


@dataclass(frozen=True)
class ThroughputPoint:
    timestamp: float  # Wall-clock seconds for the chart's time axis.
    mib_per_second: float


class ThroughputHistory:
    WINDOW_SECONDS = 10 * 60
    SMOOTHING_SAMPLES = 5

    def __init__(self) -> None:
        self.samples: deque[ThroughputPoint] = deque(maxlen=self.WINDOW_SECONDS + 1)
        self._recent_rates: deque[float] = deque(maxlen=self.SMOOTHING_SAMPLES)
        self._pending_bytes = 0
        self._last_sample_at: float | None = None
        self.has_progress = False

    def record_progress(self, bytes_sent: int) -> None:
        """Sum progress across files until the next one-second sample."""

        if bytes_sent > 0:
            self._pending_bytes += bytes_sent
            self.has_progress = True

    def sample(self, now: float, monotonic_now: float) -> ThroughputPoint:
        """Record a five-sample moving average, including zero-throughput periods."""

        elapsed = (
            max(monotonic_now - self._last_sample_at, 0.001)
            if self._last_sample_at is not None else 1.0
        )
        if elapsed > self.SMOOTHING_SAMPLES:
            self._recent_rates.clear()
        rate = self._pending_bytes / elapsed / MIB
        self._pending_bytes = 0
        self._last_sample_at = monotonic_now
        self._recent_rates.append(rate)
        point = ThroughputPoint(now, sum(self._recent_rates) / len(self._recent_rates))
        self.samples.append(point)
        while self.samples and self.samples[0].timestamp < now - self.WINDOW_SECONDS:
            self.samples.popleft()
        return point
