"""Partition `point_ledger` tự tồn tại khi ĐỒNG HỒ thật sự sang tháng mới (docs/08 §4.1).

Ràng buộc #2: hết partition = mọi sự kiện có điểm của MỌI cửa hàng bị từ chối. Các test khác
(`test_maintenance.py`) xóa partition rồi đòi một lượt bảo trì dựng lại — đúng, nhưng vẫn chạy ở
tháng HIỆN TẠI. Ở đây Postgres chạy với `libfaketime` (tests/fixtures/faketime): `now()` của DB —
thứ `ensure_point_ledger_partitions()` dựa vào — bị dời sang từng đầu tháng trong 14 tháng tới,
như thể hệ thống đã chạy liền một năm với service `central-maintenance`.

Đối chứng: cùng cách dời đồng hồ nhưng KHÔNG chạy bảo trì thì điểm chết đúng như dự đoán — test
thấy được lỗi mà nó canh, không phải xanh vì vô tình.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from central.ingest.service import ingest_batch
from central.ops import maintenance
from central.ops.provision_store import provision_store
from central.ops.seed import seed_loyalty_rules
from shared.config import ReturnRules
from shared.db import make_engine, make_session_factory, transaction
from shared.events import CustomerPayload, PointsPayload, build_envelope
from tests.integration.conftest import ROOT, _fresh_database, _run_migrations

IMAGE = "retail-sync-test/postgres-faketime:18"
DOCKERFILE_DIR = ROOT / "tests" / "fixtures" / "faketime"
STORE = "store-001"
RULES = ReturnRules(_env_file=None)


@pytest.fixture(scope="module")
def faketime_pg() -> Iterator[tuple[Any, str]]:
    testcontainers = pytest.importorskip("testcontainers.postgres")
    build = subprocess.run(  # noqa: S603 — lệnh và đường dẫn cố định của test
        ["docker", "build", "-q", "-t", IMAGE, str(DOCKERFILE_DIR)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if build.returncode != 0:
        pytest.skip(f"không dựng được image faketime: {build.stderr[-500:]}")
    with testcontainers.PostgresContainer(IMAGE, driver=None) as pg:
        dsn = (
            f"postgresql://{pg.username}:{pg.password}"
            f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
        )
        yield pg, dsn


class Clock:
    """Dời đồng hồ của CẢ server Postgres (libfaketime đọc file ở mỗi lần hỏi giờ)."""

    def __init__(self, container: Any) -> None:
        self.container = container

    def set(self, at: datetime) -> None:
        offset = int((at - datetime.now(UTC)).total_seconds())
        self.container.get_wrapped_container().exec_run(
            ["sh", "-c", f"echo '{offset:+d}' > /etc/faketimerc"]
        )

    def reset(self) -> None:
        self.container.get_wrapped_container().exec_run(["sh", "-c", "echo +0 > /etc/faketimerc"])


@pytest.fixture
async def world(
    faketime_pg: tuple[Any, str], request: pytest.FixtureRequest
) -> AsyncIterator[tuple[Clock, Any, Any]]:
    import asyncpg

    container, dsn = faketime_pg
    clock = Clock(container)
    clock.reset()  # triển khai ở HÔM NAY thật: migration tạo partition quanh tháng hiện tại
    name = f"clock_{abs(hash(request.node.nodeid)) % 10**8}"
    url = await _fresh_database(dsn, name)
    ini = Path(ROOT / "packages" / "central" / "alembic.ini")
    await asyncio.to_thread(_run_migrations, ini, dsn, name)
    engine = make_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
    factory = make_session_factory(engine)
    async with transaction(factory) as session:
        await provision_store(session, store_id=STORE, name=STORE)
        await seed_loyalty_rules(session)
    db = await asyncpg.connect(url)
    try:
        yield clock, factory, db
    finally:
        clock.reset()
        await db.close()
        await engine.dispose()


def _month(base: datetime, months: int) -> datetime:
    y, m = divmod(base.month - 1 + months, 12)
    return datetime(base.year + y, m + 1, 1, tzinfo=UTC)


async def _earn(factory: Any, *, occurred_at: datetime) -> tuple[uuid.UUID, Any]:
    """Khách mới + một lần tích điểm, qua ĐÚNG đường ingest của `POST /events`."""
    customer_id, points_id = uuid.uuid4(), uuid.uuid4()
    events = [
        build_envelope(
            event_id=uuid.uuid4(),
            event_type="CustomerCreated",
            store_id=STORE,
            occurred_at=occurred_at,
            payload=CustomerPayload(
                customer_id=customer_id,
                phone_hash=f"hash-{customer_id.hex}",
                created_locally_at_store=STORE,
            ),
        ),
        build_envelope(
            event_id=points_id,
            event_type="PointsEarned",
            store_id=STORE,
            occurred_at=occurred_at,
            payload=PointsPayload(customer_id=customer_id, delta=7, reason="EARN"),
        ),
    ]
    async with transaction(factory) as session:
        result = await ingest_batch(session, events, store_id=STORE, rules=RULES)
    return points_id, result


async def _partition_of(db: Any, event_id: uuid.UUID) -> str | None:
    value: str | None = await db.fetchval(
        "SELECT tableoid::regclass::text FROM point_ledger WHERE event_id = $1", event_id
    )
    return value


async def test_a_year_of_month_boundaries_with_the_maintenance_schedule(world: Any) -> None:
    """14 lần sang tháng. Mỗi lần: ngay sau nửa đêm đầu tháng có một lượt bảo trì (service chạy
    mỗi giờ), rồi hai cửa hàng gửi điểm — một cửa hàng bán lúc 00:01, một cửa hàng offline từ tối
    hôm trước (điểm thuộc THÁNG TRƯỚC, đồng bộ vào tháng này: lý do của `months_back`)."""
    clock, factory, db = world
    today = datetime.now(UTC)
    for step in range(1, 15):
        start = _month(today, step)
        clock.set(start + timedelta(seconds=30))
        result = await maintenance.run_once(factory, rules=RULES, months_ahead=3)
        assert result.errors == {}, (start, result.errors)
        assert result.partitions is not None and result.partitions.is_healthy, start
        assert result.partitions.months_ahead >= 3

        on_time, r1 = await _earn(factory, occurred_at=start + timedelta(minutes=1))
        late, r2 = await _earn(factory, occurred_at=start - timedelta(hours=3))
        assert r1.rejected == [] and r2.rejected == [], (start, r1.rejected, r2.rejected)
        assert await _partition_of(db, on_time) == f"point_ledger_{start:%Y_%m}"
        previous = start - timedelta(days=1)
        assert await _partition_of(db, late) == f"point_ledger_{previous:%Y_%m}"

    # Chỉ số dashboard/cảnh báo (`point_ledger_partition_months_ahead`) đọc cùng đồng hồ.
    assert await db.fetchval("SELECT months_ahead FROM point_ledger_partition_health") >= 3


async def test_without_maintenance_points_die_when_the_clock_passes_the_last_partition(
    world: Any,
) -> None:
    """Đối chứng — đúng sự cố mà ràng buộc #2 cảnh báo, và đúng tình trạng hệ thống tới
    2026-09-25 (không có gì gọi hàm tạo partition): chạy tốt tới tháng cuối có partition, rồi mọi
    sự kiện có điểm bị từ chối vĩnh viễn (vào dead-letter). Một lượt bảo trì là hồi phục."""
    clock, factory, db = world
    past_last = _month(datetime.now(UTC), 5)  # migration tạo tới +3 tháng
    clock.set(past_last + timedelta(hours=1))
    assert await db.fetchval("SELECT months_ahead FROM point_ledger_partition_health") < 2

    points_id, result = await _earn(factory, occurred_at=past_last + timedelta(minutes=5))
    [rejected] = result.rejected
    assert rejected.event_id == points_id and not rejected.retryable  # dead-letter, không thử lại
    assert await _partition_of(db, points_id) is None

    healed = await maintenance.run_once(factory, rules=RULES, months_ahead=3)
    assert healed.partitions is not None and healed.partitions.is_healthy
    again, result = await _earn(factory, occurred_at=past_last + timedelta(minutes=6))
    assert result.rejected == []
    assert await _partition_of(db, again) == f"point_ledger_{past_last:%Y_%m}"
