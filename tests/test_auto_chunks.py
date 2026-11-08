"""Deterministic checks of single and parallel chunk-size trials."""

from irods_client_system_tray.auto_chunks import AutomaticChunkSize


def record_window(tuner, start, *, duration=2, count=6, size=1024, workers=2):
    change = None
    for index in range(count):
        began = start + index * duration
        change = tuner.record_success(
            size, began, began + duration, backlogged=True, workers=workers
        )
    return change


def test_single_chunk_trials_accept_improvement_and_reject_regression():
    tuner = AutomaticChunkSize(8)

    assert record_window(tuner, 0) == 16
    assert tuner.probing
    assert record_window(tuner, 12, duration=1.6, count=8) is None
    assert tuner.current == 16
    assert not tuner.probing
    assert record_window(tuner, 25) is None  # Cooldown.
    assert record_window(tuner, 70) == 8
    assert record_window(tuner, 82) == 16  # Slower trial is reverted.


def test_parallel_candidates_respect_memory_budget():
    tuner = AutomaticChunkSize(8, parallel=True)

    assert record_window(tuner, 0, workers=10) == 4
    assert tuner.current == 4  # 10 * 3 * 16 MiB exceeds 256 MiB.
    tuner = AutomaticChunkSize(16, parallel=True)
    assert tuner.limit_for_workers(10) == 8
    assert tuner.limit_for_workers(22) == 8  # No size fits; controller caps workers.


def test_parallel_candidates_use_configured_memory_cap():
    tuner = AutomaticChunkSize(16, parallel=True, memory_cap_mib=48)
    assert tuner.limit_for_workers(2) == 8
    assert record_window(tuner, 0, workers=2) == 4
    tuner.memory_cap_mib = 96
    assert tuner.limit_for_workers(2) == 4


def test_parallel_probe_reverts_after_transfer_failure_but_not_authentication_error():
    tuner = AutomaticChunkSize(4, parallel=True)
    assert record_window(tuner, 0) == 8

    assert tuner.record_failure("Invalid credentials", 13) is None
    assert tuner.record_failure("connection reset", 14) == 4
    assert not tuner.probing
    assert record_window(tuner, 20) is None  # Cooldown before retrying a size.


def test_chunk_tuner_needs_a_backlog_and_comparable_sizes():
    tuner = AutomaticChunkSize(8)
    for index in range(9):
        assert tuner.record_success(100, index, index + 2, backlogged=False, workers=2) is None
    assert tuner.current == 8

    assert record_window(tuner, 20) == 16
    assert record_window(tuner, 32, size=20_000) is None
    assert tuner.probing
    assert record_window(tuner, 44) == 8
