"""Deterministic checks of the aggregate-throughput worker tuner."""

import pytest

from irods_client_system_tray.auto_workers import AutomaticUploadWorkers


def record_window(tuner, *, start: float, duration: float = 2, size: int = 1024, count=None):
    change = None
    for index in range(count or tuner.MIN_SAMPLES):
        began = start + index * duration
        change = tuner.record_success(size, began, began + duration, backlogged=True)
    return change


def test_auto_worker_tuner_waits_for_sustained_backlog():
    tuner = AutomaticUploadWorkers(ceiling=4)

    for index in range(10):
        assert tuner.record_success(1024, index, index + 1, backlogged=False) is None
    assert tuner.current == 2
    for index in range(tuner.MIN_SAMPLES - 1):
        assert tuner.record_success(1024, index * 2, index * 2 + 2, backlogged=True) is None
    assert tuner.current == 2

    assert tuner.record_success(1024, 10, 12, backlogged=True) == 3
    assert tuner.current == 3
    assert tuner.recent_throughput == 6 * 1024 / 12


def test_auto_worker_tuner_reverts_unhelpful_probe_and_tests_lower_limit():
    tuner = AutomaticUploadWorkers(ceiling=3)

    assert record_window(tuner, start=0) == 3
    assert record_window(tuner, start=12) == 2
    assert tuner.current == 2
    assert record_window(tuner, start=24) is None  # Cooldown after rejection.
    assert record_window(tuner, start=90) == 1
    assert record_window(tuner, start=102) == 2


def test_auto_worker_tuner_keeps_a_meaningfully_faster_probe():
    tuner = AutomaticUploadWorkers(ceiling=4)

    assert record_window(tuner, start=0) == 3
    # The same-sized files finishing sooner means higher aggregate throughput.
    assert record_window(tuner, start=12, duration=1.6, count=8) is None
    assert tuner.current == 3
    assert tuner.recent_throughput == pytest.approx(8 * 1024 / 12.8)
    assert record_window(tuner, start=24) is None  # Don't probe again immediately.
    assert record_window(tuner, start=60) == 4


def test_auto_worker_tuner_does_not_compare_very_different_file_sizes():
    tuner = AutomaticUploadWorkers(ceiling=3)

    assert record_window(tuner, start=0) == 3
    assert record_window(tuner, start=12, size=10_000) is None
    assert tuner.current == 3
    assert record_window(tuner, start=24) == 2


def test_auto_worker_tuner_backs_off_on_distinct_network_failures():
    tuner = AutomaticUploadWorkers(ceiling=4, initial=3)

    assert tuner.record_failure("a", "Invalid credentials", 1, backlogged=True) is None
    assert tuner.record_failure("a", "connection reset", 2, backlogged=True) is None
    assert tuner.record_failure("a", "connection reset", 3, backlogged=True) is None
    assert tuner.record_failure("b", "timed out", 4, backlogged=True) == 2
    assert tuner.current == 2
    assert record_window(tuner, start=5) is None  # Wait before probing again.


def test_auto_worker_tuner_caps_to_ceiling_and_resets_without_backlog():
    tuner = AutomaticUploadWorkers(ceiling=1)
    assert tuner.current == 1

    tuner = AutomaticUploadWorkers(ceiling=4, initial=3)
    assert tuner.set_ceiling(2) == 2
    assert tuner.record_success(100, 0, 1, backlogged=False) is None
    assert tuner.current == 2


def test_auto_worker_tuner_keeps_last_rate_but_requires_fresh_backlog_samples():
    tuner = AutomaticUploadWorkers(ceiling=3)
    assert record_window(tuner, start=0) == 3
    previous_rate = tuner.recent_throughput

    tuner.record_success(1024, 12, 14, backlogged=False)

    assert tuner.recent_throughput == previous_rate
    assert tuner.current == 3


def test_network_errors_must_be_close_enough_to_justify_backoff():
    tuner = AutomaticUploadWorkers(ceiling=4, initial=3)

    assert tuner.record_failure("one", "connection reset", 0, backlogged=True) is None
    assert tuner.record_failure("two", "timed out", 100, backlogged=True) is None
    assert tuner.record_failure("three", "connection refused", 110, backlogged=True) == 2


def test_fast_file_transfers_can_trigger_tuning_after_sustained_backlog():
    tuner = AutomaticUploadWorkers(ceiling=3)

    for index in range(105):
        changed = tuner.record_success(1024, index * 0.1, (index + 1) * 0.1, backlogged=True)
        if changed is not None:
            break

    assert tuner.current == 3
    assert tuner.recent_throughput is not None
