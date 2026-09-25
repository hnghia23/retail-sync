"""Chế độ `virtual` của bộ giả lập — phần thuần: tật, đồng hồ tổng hợp, ranh giới import."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from simulator.generator import Sale, plan_store
from simulator.profile import load_profile
from simulator.quirks import Quirks, mark_concurrent_customers, offline_stores
from simulator.sinks.virtual import _Clock, read_keys

ROOT = Path(__file__).resolve().parents[2]
CATALOG = {f"P{i}": 10_000 + 1_000 * i for i in range(50)}


def test_quirks_parse_names_and_typed_overrides() -> None:
    q = Quirks.parse("offline, resend", ["offline_share=0.5", "offline_from_day=0"])
    assert q.names() == ["offline", "resend"]
    assert (q.offline_share, q.offline_from_day) == (0.5, 0)
    with pytest.raises(ValueError, match="tật không biết"):
        Quirks.parse("offline,typo")
    with pytest.raises(ValueError, match="không hợp lệ"):
        Quirks.parse("offline", ["offline=0"])  # cờ bật/tắt không phải tham số


def test_offline_window_spans_the_night_in_opening_hours_only() -> None:
    """18:00 hôm nay → 12:00 mai = 4 giờ tối nay + 5 giờ sáng mai, đêm không tồn tại."""
    profile = load_profile("t0")  # 07:00–22:00
    lo, hi = Quirks(offline=True).offline_window(profile, days=3)
    day = 15 * 3600
    assert lo == 1 * day + 11 * 3600  # ngày 1 (áp chót), 18:00
    assert hi == 2 * day + 5 * 3600  # ngày 2 (cuối), 12:00
    with pytest.raises(ValueError, match="rỗng"):
        Quirks(offline=True, offline_to_day=-2, offline_to_hour=8).offline_window(profile, 3)


def test_offline_stores_is_deterministic_and_at_least_one() -> None:
    ids = [f"s{i}" for i in range(20)]
    picked = offline_stores(ids, Quirks(offline=True), seed=7)
    assert picked == offline_stores(ids, Quirks(offline=True), seed=7)
    assert len(picked) == 5
    assert len(offline_stores(ids[:2], Quirks(offline=True, offline_share=0.01), seed=7)) == 1
    assert offline_stores(ids, Quirks(), seed=7) == []


def test_concurrent_customer_moves_only_returning_customers_to_another_store() -> None:
    profile = dataclasses.replace(load_profile("t0"), sales_per_store_day=200)
    plans = {
        sid: plan_store(
            profile,
            store_id=sid,
            catalog=list(CATALOG),
            prices=CATALOG,
            seed=1,
            nonce=1,
            days=2,
            rate=1.0,
            start_date=date(2026, 8, 30),
        )
        for sid in ("a", "b")
    }
    marked = mark_concurrent_customers(
        plans, Quirks(concurrent_customer=True, concurrent_share=0.3), seed=1
    )
    moved = 0
    for sid, intents in marked.items():
        for before, after in zip(plans[sid], intents, strict=True):
            if not isinstance(after, Sale) or after.customer is None:
                continue
            assert isinstance(before, Sale) and before.customer is not None
            if before.customer.new:
                assert after.customer == before.customer  # khách mới không bao giờ bị đổi
            elif after.customer.ref != before.customer.ref:
                moved += 1
                assert not after.customer.ref.startswith(f"{sid}-")  # khách của cửa hàng KHÁC
    assert moved > 0
    assert mark_concurrent_customers(plans, Quirks(), seed=1) is plans


def test_clock_maps_synthetic_seconds_to_store_time_across_days() -> None:
    """Giây giả lập chỉ đếm giờ mở cửa: hết 15 giờ của ngày 0 là 07:00 ngày 1."""
    profile = load_profile("t0")
    clock = _Clock(profile, date(2026, 8, 31), rate=10.0, utc_offset=timedelta(hours=7))
    at, day = clock.at(0.0)
    assert (at, day) == (datetime(2026, 8, 31, 0, 0, tzinfo=UTC), date(2026, 8, 31))  # 07:00 VN
    at, day = clock.at(15 * 3600 / 10 + 60 / 10)  # ngày 1, 07:01 giờ VN
    assert (at, day) == (datetime(2026, 9, 1, 0, 1, tzinfo=UTC), date(2026, 9, 1))


def test_read_keys_skips_comments_and_rejects_garbage() -> None:
    keys = read_keys("# secret\nstore-a=k1\n\n store-b = k2 \n")
    assert [(k.store_id, k.api_key) for k in keys] == [("store-a", "k1"), ("store-b", "k2")]
    with pytest.raises(ValueError):
        read_keys("store-a\n")


def test_virtual_sink_pulls_in_no_database_or_web_framework() -> None:
    """Hợp đồng import-linter chỉ thấy module của repo. Lớp này kiểm phần còn lại: bộ giả lập
    dùng lại `edge.sync.client` + domain, và chúng phải THUẦN — không kéo DB hay FastAPI theo."""
    code = (
        "import sys, simulator.sinks.virtual, simulator.quirks;"
        "print(','.join(m for m in ('sqlalchemy', 'fastapi', 'asyncpg', 'alembic')"
        " if m in sys.modules))"
    )
    result = subprocess.run(  # noqa: S603 — lệnh cố định
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "packages"), "SYSTEMROOT": _systemroot()},
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == ""


def _systemroot() -> str:
    import os

    return os.environ.get("SYSTEMROOT", "")


async def test_audit_clickhouse_reads_survive_a_transient_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Đứt kết nối tạm thời (gặp trên compose lúc DAG dựng lại bảng) không được làm cả lần đối
    soát chết: đọc là idempotent nên thử lại. Hết lượt thì vẫn ném lỗi, không nuốt."""
    import httpx

    from simulator import audit as audit_mod

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadError("connection reset", request=request)
        return httpx.Response(200, text="1\t2\n")

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "simulator.audit.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr("simulator.audit.asyncio.sleep", _no_sleep)
    target = audit_mod.ClickHouseTarget(url="http://ch/", user="u", password="p")
    assert await audit_mod._ch_rows(target, "SELECT 1") == [["1", "2"]]
    assert calls["n"] == 3

    calls["n"] = -10  # mọi lần đều lỗi
    with pytest.raises(httpx.ReadError):
        await audit_mod._ch_rows(target, "SELECT 1")


async def _no_sleep(_: float) -> None:
    return None


# ═══════════════════════ Độ tươi trong báo cáo đối soát (docs/17 §6) ═══════════════════════


def test_audit_freshness_measures_each_layer_against_the_freshest_one() -> None:
    from datetime import UTC, datetime, timedelta

    from simulator.audit import _Sale, freshness

    now = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)

    def sales(*minutes_ago: int) -> dict[str, _Sale]:
        return {
            str(i): _Sale(1, "2026-09-25", {}, 0, now - timedelta(minutes=m))
            for i, m in enumerate(minutes_ago)
        }

    out = freshness({"L1": sales(30, 2), "L2": sales(30, 2), "L3": sales(30), "L4": {}}, now=now)

    assert set(out) == {"L1", "L2", "L3"}  # L4 chưa có đơn nào: vắng, không phải 0
    assert out["L1"]["freshness_seconds"] == 120
    assert out["L2"]["behind_seconds"] == 0
    assert out["L3"]["behind_seconds"] == 28 * 60
    # Cửa hàng ảo: L1 là manifest, không có occurred_at → tầng tươi nhất là L2.
    virtual = freshness({"L1": {"a": _Sale(1, "d", {}, 0)}, "L2": sales(5)}, now=now)
    assert set(virtual) == {"L2"} and virtual["L2"]["behind_seconds"] == 0


def test_audit_observations_map_status_findings_and_freshness() -> None:
    from simulator.audit import AuditReport, Finding
    from simulator.telemetry import observations

    report = AuditReport(
        status="DIVERGED",
        findings=[
            Finding("DIVERGED", "s1", "L1→L2", "thiếu đơn"),
            Finding("CONVERGING", "s1", "L2→L3", "thiếu đơn"),
            Finding("CONVERGING", "s1", "L2→L3", "thiếu đơn"),
        ],
        freshness={"s1": {"L2": {"newest": "x", "freshness_seconds": 90.0, "behind_seconds": 5}}},
    )
    obs = observations(report)

    assert obs["audit_status"] == [(2.0, {})]
    assert {labels["level"]: v for v, labels in obs["audit_findings"]} == {
        "CONVERGING": 2.0,
        "DIVERGED": 1.0,
    }
    assert obs["audit_freshness"] == [(90.0, {"layer": "L2", "store_id": "s1"})]
    assert obs["audit_behind"] == [(5.0, {"layer": "L2", "store_id": "s1"})]
