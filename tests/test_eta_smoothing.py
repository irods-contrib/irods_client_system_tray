"""The ETA history ignores brief spikes but responds to real queue changes."""

from irods_client_system_tray.eta_smoothing import EtaSmoother


def test_five_second_eta_median_ignores_a_spike_and_expires_old_samples():
    smoother = EtaSmoother()
    for second, estimate in enumerate((10, 11, 120, 12, 10)):
        shown = smoother.display(estimate, 1000, second, sample=True)

    assert shown == 11
    assert smoother.display(500, 1000, 4.5) == 11  # No new sample on callback bursts.
    assert smoother.display(50, 1000, 10) == 50  # No old ETAs after the window expires.


def test_queue_growth_completion_and_unknown_speed_reset_eta_immediately():
    smoother = EtaSmoother()
    assert smoother.display(10, 1000, 0, sample=True) == 10
    assert smoother.display(60, 2000, 1) == 60  # New queued work.
    assert smoother.display(0, 0, 2) == 0
    assert smoother.display(30, 1000, 3, sample=True) == 30
    assert smoother.display(None, 1000, 4) is None
    assert smoother.display(50, 1000, 5, sample=True) == 50
