"""Bounded, in-memory tuning for iRODS single and parallel PUT sizes."""

from __future__ import annotations

from collections import deque
from statistics import median

from .auto_workers import TransferSample


CHUNK_SIZES_MIB = (4, 8, 16)
PARALLEL_TRANSFER_THREADS = 3
PARALLEL_MEMORY_BUDGET_MIB = 256


class AutomaticChunkSize:
    """Compare aggregate throughput at nearby sizes with a fixed worker count."""

    MIN_SAMPLES = 6
    MIN_WINDOW_SECONDS = 10.0
    IMPROVEMENT_RATIO = 1.10
    PROBE_COOLDOWN_SECONDS = 30.0
    REJECT_COOLDOWN_SECONDS = 60.0

    def __init__(self, initial: int, *, parallel: bool = False) -> None:
        self.current = initial
        self.parallel = parallel
        self._samples: deque[TransferSample] = deque(maxlen=8)
        self._window_started_at: float | None = None
        self._reference: tuple[float, float] | None = None
        self._probe_origin: int | None = None
        self._rejected: set[int] = set()
        self._next_probe_at = 0.0

    @property
    def probing(self) -> bool:
        return self._probe_origin is not None

    def clear_window(self) -> None:
        """Ignore transfers begun with the preceding size or worker limit."""

        self._samples.clear()
        self._window_started_at = None

    def reset(self) -> None:
        self.clear_window()
        self._reference = None
        self._probe_origin = None
        self._rejected.clear()

    def _allowed(self, size: int, workers: int) -> bool:
        return size in CHUNK_SIZES_MIB and (
            not self.parallel
            or workers * PARALLEL_TRANSFER_THREADS * size <= PARALLEL_MEMORY_BUDGET_MIB
        )

    def limit_for_workers(self, workers: int) -> int:
        """Lower parallel size if the effective worker count exceeds the memory budget."""

        if not self.parallel or self._allowed(self.current, workers):
            return self.current
        sizes = [size for size in CHUNK_SIZES_MIB if self._allowed(size, workers)]
        if not sizes:
            return self.current  # No feasible size; the controller must cap workers.
        self.current = max(sizes)
        self.reset()
        return self.current

    def record_success(
        self, size_bytes: int, started: float, finished: float, *,
        backlogged: bool, workers: int,
    ) -> int | None:
        if not backlogged:
            self.clear_window()
            return None
        if size_bytes <= 0 or started >= finished:
            return None
        if self._window_started_at is None:
            self._window_started_at = finished
        self._samples.append(TransferSample(size_bytes, started, finished))
        if len(self._samples) < self.MIN_SAMPLES:
            return None
        if finished - self._window_started_at < self.MIN_WINDOW_SECONDS:
            return None

        elapsed = max(sample.finished_at for sample in self._samples) - min(
            sample.started_at for sample in self._samples
        )
        throughput = sum(sample.bytes_transferred for sample in self._samples) / elapsed
        typical_size = median(sample.bytes_transferred for sample in self._samples)
        self.clear_window()

        if self.probing and self._reference is not None:
            baseline, baseline_size = self._reference
            if not 0.5 <= typical_size / baseline_size <= 2:
                return None
            if throughput >= baseline * self.IMPROVEMENT_RATIO:
                self._probe_origin = None
                self._reference = (throughput, typical_size)
                self._rejected.clear()
                self._next_probe_at = finished + self.PROBE_COOLDOWN_SECONDS
                return None
            self._rejected.add(self.current)
            self.current = self._probe_origin
            self._probe_origin = None
            self._reference = None
            self._next_probe_at = finished + self.REJECT_COOLDOWN_SECONDS
            return self.current

        if finished < self._next_probe_at:
            return None
        self._reference = (throughput, typical_size)
        for candidate in (self.current * 2, self.current // 2):
            if self._allowed(candidate, workers) and candidate not in self._rejected:
                self._probe_origin = self.current
                self.current = candidate
                return candidate
        self._rejected.clear()
        self._next_probe_at = finished + self.REJECT_COOLDOWN_SECONDS * 5
        return None

    def record_failure(self, message: str, now: float) -> int | None:
        """Revert a probe on transfer errors, not authentication/policy failures."""

        if not self.probing:
            return None
        transfer_errors = (
            "connection reset", "connection refused", "timed out", "timeout",
            "broken pipe", "unable to send message", "parallel put failed",
        )
        if not any(error in message.lower() for error in transfer_errors):
            return None
        self._rejected.add(self.current)
        self.current = self._probe_origin
        self._probe_origin = None
        self._reference = None
        self.clear_window()
        self._next_probe_at = now + self.REJECT_COOLDOWN_SECONDS
        return self.current
