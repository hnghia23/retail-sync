"""Lịch quét TOÀN BỘ INV-4 của `central-maintenance` (docs/08 §4.2, `FullScanPolicy`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from central.ops.maintenance import FullScanPolicy

POLICY = FullScanPolicy(every_days=30, start_hour=1, end_hour=5, utc_offset_minutes=420)
#: 02:00 giờ cửa hàng (UTC+7) = 19:00 UTC hôm trước.
OFF_PEAK = datetime(2026, 9, 24, 19, 0, tzinfo=UTC)
PEAK = datetime(2026, 9, 25, 11, 0, tzinfo=UTC)  # 18:00 giờ cửa hàng


def test_never_scanned_runs_at_the_first_off_peak_hour() -> None:
    assert POLICY.due(None, OFF_PEAK)
    assert not POLICY.due(None, PEAK)  # không quét toàn bộ giữa giờ bán


def test_due_only_after_the_interval() -> None:
    assert not POLICY.due(OFF_PEAK - timedelta(days=29), OFF_PEAK)
    assert POLICY.due(OFF_PEAK - timedelta(days=30), OFF_PEAK)


def test_off_peak_window_is_in_store_local_time() -> None:
    assert POLICY.off_peak(datetime(2026, 9, 24, 18, 0, tzinfo=UTC))  # 01:00 VN
    assert not POLICY.off_peak(datetime(2026, 9, 24, 22, 0, tzinfo=UTC))  # 05:00 VN


def test_zero_interval_disables_the_full_scan() -> None:
    assert not FullScanPolicy(every_days=0).due(None, OFF_PEAK)
