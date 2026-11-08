"""Conservative, in-memory tuning of simultaneous file uploads."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class TransferSample:
    bytes_transferred: int
    started_at: float
    finished_at: float


class AutomaticUploadWorkers:
    """Compare aggregate throughput during backlogged windows at nearby limits.

    Never interrupt a transfer: callers apply a new limit only when dispatching
    subsequent work. Observations are deliberately not persisted across sessions.
    """

    MIN_SAMPLES = 6
    MIN_WINDOW_SECONDS = 10.0
    IMPROVEMENT_RATIO = 1.10
    PROBE_COOLDOWN_SECONDS = 30.0
    REJECT_COOLDOWN_SECONDS = 60.0
    ERROR_COOLDOWN_SECONDS = 120.0

    def __init__(self, ceiling: int, *, initial: int = 2) -> None:
        self.ceiling = max(1, ceiling)
        self.current = min(max(1, initial), self.ceiling)
        self.recent_throughput: float | None = None
        self._samples: deque[TransferSample] = deque(maxlen=8)
        self._window_started_at: float | None = None
        self._reference: tuple[float, float] | None = None
        self._probe_origin: int | None = None
        self._rejected: set[int] = set()
        self._next_probe_at = 0.0
        self._network_failures: dict[str, float] = {}

    def reset_observations(self, *, clear_throughput: bool = True) -> None:
        """Drop measurements that no longer describe a sustained backlog."""

        self._samples.clear()
        self._window_started_at = None
        self._reference = None
        self._probe_origin = None
        self._network_failures.clear()
        if clear_throughput:
            self.recent_throughput = None

    def set_ceiling(self, ceiling: int) -> int:
        self.ceiling = max(1, ceiling)
        if self.current > self.ceiling:
            self.current = self.ceiling
            self.reset_observations()
        return self.current

    def record_success(
        self, bytes_transferred: int, started_at: float, finished_at: float, *, backlogged: bool
    ) -> int | None:
        """Evaluate the last few transfers after enough work spans a useful interval."""

        if not backlogged:
            self.reset_observations(clear_throughput=False)
            return None
        if bytes_transferred <= 0 or started_at >= finished_at:
            return None
        if self._window_started_at is None:
            self._window_started_at = finished_at
        self._samples.append(TransferSample(bytes_transferred, started_at, finished_at))
        if len(self._samples) < self.MIN_SAMPLES:
            return None
        if finished_at - self._window_started_at < self.MIN_WINDOW_SECONDS:
            return None
        elapsed = max(sample.finished_at for sample in self._samples) - min(
            sample.started_at for sample in self._samples
        )

        throughput = sum(sample.bytes_transferred for sample in self._samples) / elapsed
        typical_size = median(sample.bytes_transferred for sample in self._samples)
        self.recent_throughput = throughput
        self._samples.clear()
        self._window_started_at = None

        if self._network_failures:
            self._network_failures.clear()
            self._next_probe_at = finished_at + self.REJECT_COOLDOWN_SECONDS
            return None

        if self._probe_origin is not None and self._reference is not None:
            baseline, baseline_size = self._reference
            if not 0.5 <= typical_size / baseline_size <= 2.0:
                return None  # Do not compare unlike file-size mixes.
            if throughput >= baseline * self.IMPROVEMENT_RATIO:
                self._probe_origin = None
                self._reference = (throughput, typical_size)
                self._rejected.clear()
                self._next_probe_at = finished_at + self.PROBE_COOLDOWN_SECONDS
                return None

            self._rejected.add(self.current)
            self.current = self._probe_origin
            self._probe_origin = None
            self._reference = None
            self._next_probe_at = finished_at + self.REJECT_COOLDOWN_SECONDS
            return self.current

        if finished_at < self._next_probe_at:
            return None
        self._reference = (throughput, typical_size)
        candidates = (self.current + 1, self.current - 1)
        for candidate in candidates:
            if 1 <= candidate <= self.ceiling and candidate not in self._rejected:
                self._probe_origin = self.current
                self.current = candidate
                return self.current
        self._next_probe_at = finished_at + self.REJECT_COOLDOWN_SECONDS * 5
        self._rejected.clear()
        return None

    def record_failure(self, path: str, message: str, now: float, *, backlogged: bool) -> int | None:
        """Back off after distinct network failures, not authentication or policy errors."""

        if not backlogged:
            self.reset_observations()
            return None
        network_errors = (
            "connection reset", "connection refused", "timed out", "timeout",
            "broken pipe", "network is unreachable", "unable to send message",
        )
        if not any(error in message.lower() for error in network_errors):
            return None
        self._network_failures = {
            failed_path: when for failed_path, when in self._network_failures.items()
            if now - when <= 60
        }
        self._network_failures[path] = now
        if len(self._network_failures) < 2:
            return None

        old_count = self.current
        self.current = max(1, self.current - 1)
        self.reset_observations()
        self._next_probe_at = now + self.ERROR_COOLDOWN_SECONDS
        return self.current if self.current != old_count else None
