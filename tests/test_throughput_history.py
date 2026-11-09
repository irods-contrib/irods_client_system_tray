"""Deterministic tests for the session-only, ten-minute upload-rate history."""

import pytest

from irods_client_system_tray.throughput_history import MIB, ThroughputHistory


def test_combined_progress_is_smoothed_over_five_samples_and_idle_reaches_zero():
    history = ThroughputHistory()
    assert not history.has_progress
    history.record_progress(MIB)
    history.record_progress(3 * MIB)
    history.record_progress(-MIB)  # Repeated/reduced progress is not upload traffic.
    assert history.has_progress

    assert history.sample(100, 10).mib_per_second == pytest.approx(4.0)
    for second in range(1, 5):
        assert history.sample(100 + second, 10 + second).mib_per_second == pytest.approx(
            4 / (second + 1)
        )
    assert history.sample(105, 15).mib_per_second == 0
    assert len(history.samples) == 6


def test_history_keeps_only_the_last_ten_wall_clock_minutes():
    history = ThroughputHistory()
    history.record_progress(2 * MIB)
    history.sample(1000, 50)
    history.sample(1500, 550)
    assert len(history.samples) == 2
    assert history.samples[-1].mib_per_second == 0

    history.sample(1601, 651)
    assert [sample.timestamp for sample in history.samples] == [1500, 1601]


def test_history_does_not_carry_stale_rates_across_long_pause():
    history = ThroughputHistory()
    history.record_progress(MIB)
    history.sample(10, 10)

    assert history.sample(20, 20).mib_per_second == 0
