"""Smooth the displayed ETA independently of the throughput estimate."""

from __future__ import annotations

from collections import deque
from math import ceil
from statistics import median


class EtaSmoother:
    WINDOW_SECONDS = 5

    def __init__(self) -> None:
        self._samples: deque[tuple[float, int]] = deque(maxlen=self.WINDOW_SECONDS)
        self._last_remaining_bytes: int | None = None

    def display(
        self, estimate: int | None, remaining_bytes: int | None, now: float, *,
        sample: bool = False,
    ) -> int | None:
        """Return a rolling median, recording at most one estimate per second."""

        if estimate is None or estimate == 0:
            self._samples.clear()
            self._last_remaining_bytes = remaining_bytes
            return estimate

        if (
            remaining_bytes is not None and self._last_remaining_bytes is not None
            and remaining_bytes > self._last_remaining_bytes
        ):
            # Newly queued work must not inherit an ETA for a smaller queue.
            self._samples.clear()
        self._last_remaining_bytes = remaining_bytes
        while self._samples and now - self._samples[0][0] >= self.WINDOW_SECONDS:
            self._samples.popleft()
        if sample:
            self._samples.append((now, estimate))
        return ceil(median(value for _, value in self._samples)) if self._samples else estimate
