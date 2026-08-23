"""Tests for the persistent rotating activity log."""

from __future__ import annotations

from irods_client_system_tray.activity_log import ActivityLog


def test_activity_log_reads_recent_entries_newest_first(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    try:
        log.append("older event")
        log.append("newer event")

        recent = log.read_recent()
    finally:
        log.close()

    assert " - newer event" in recent[0]
    assert " - older event" in recent[1]


def test_activity_log_reads_across_rotated_files(tmp_path):
    log = ActivityLog(
        tmp_path / "activity.log",
        max_bytes=250,
        backup_count=2,
        visible_entry_count=10,
    )
    try:
        for index in range(20):
            log.append(f"event {index:02d} " + "x" * 80)

        recent = log.read_recent()
    finally:
        log.close()

    assert (tmp_path / "activity.log.1").exists()
    assert " - event 19 " in recent[0]
    assert len(recent) <= 10
