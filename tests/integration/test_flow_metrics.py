"""Metric của chặng S2 (outbox cửa hàng) và S3 (ingest trung tâm) trên Postgres THẬT.

docs/17 §6, docs/08 §5. Dashboard "sức khỏe luồng" và ngưỡng cảnh báo đọc đúng các con số này,
nên chúng phải đếm đúng thứ docs định nghĩa — đặc biệt ở hai chỗ dễ sai mà không có lỗi nào:
  - dead-letter lẫn vào "đang chờ" làm `oldest_unsent_age_seconds` tăng mãi (báo động giả vĩnh
    viễn cho chỉ số quan trọng nhất);
  - đếm trùng/mới lẫn nhau, hoặc đếm cả lô bị rollback.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from central.ingest import router as ingest_router
from central.ingest.service import IngestStats
from edge.health import read_outbox
from shared.types import utcnow
from tests.integration.conftest import Central
from tests.integration.test_sync_pipeline import (
    STORE_ID,
    _outbox_envelopes,
    _seed_edge,
    _sell,
)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, IngestStats]]:
    calls: list[tuple[str, IngestStats]] = []
    monkeypatch.setattr(ingest_router, "record_batch", lambda s, st: calls.append((s, st)))
    return calls


async def test_ingest_counts_new_then_duplicate_then_poison(
    central: Central, edge: Any, edge_db: Any, recorded: list[tuple[str, IngestStats]]
) -> None:
    """`event_duplicate_rate` (docs/08 §5) chỉ có nghĩa nếu "trùng" tách khỏi "mới", dù cả hai
    đều là `accepted` với cửa hàng (AT-03)."""
    await _sell(edge, await _seed_edge(edge_db))
    envelopes = await _outbox_envelopes(edge_db)
    client = central.client()

    await client.push(envelopes)
    await client.push(envelopes)
    poison = dict(envelopes[0], event_id=str(uuid.uuid4()), schema_version=99)
    await client.push([poison])

    assert [(s, st.outcomes()) for s, st in recorded] == [
        (STORE_ID, _only(accepted=2)),
        (STORE_ID, _only(duplicate=2)),
        (STORE_ID, _only(rejected_permanent=1)),
    ]


async def test_refused_batch_is_counted_and_not_recorded_as_events(
    central: Central,
    edge: Any,
    edge_db: Any,
    recorded: list[tuple[str, IngestStats]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused: list[str] = []
    monkeypatch.setattr(ingest_router, "record_refused", refused.append)
    await _sell(edge, await _seed_edge(edge_db))
    central.app.state.ingest_gate = asyncio.Semaphore(0)  # quá tải: mọi chỗ đã bị chiếm

    # Gọi thẳng HTTP, không qua `HttpCentralClient`: client đổi 503 thành exception, và một
    # `except` rộng ở đây từng nuốt mất nguyên nhân khi test đỏ lúc chạy cả suite.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=central.app), base_url="http://central"
    ) as http:
        response = await http.post(
            "/api/v1/events",
            json=await _outbox_envelopes(edge_db),
            headers={"Authorization": f"Bearer {central.keys[STORE_ID]}"},
        )

    assert response.status_code == 503, response.text
    assert refused == ["overloaded"]
    assert recorded == []


async def test_outbox_snapshot_keeps_dead_letters_out_of_pending(edge: Any, edge_db: Any) -> None:
    """Bản đầu của `/health/outbox` đếm mọi dòng `sent_at IS NULL`: một sự kiện dead-letter từ
    hôm qua làm "tuổi sự kiện chờ cũ nhất" = 1 ngày, mãi mãi, dù hàng đợi thật trống trơn."""
    await _sell(edge, await _seed_edge(edge_db))
    dead_id = uuid.uuid4()
    await edge_db.execute(
        "INSERT INTO outbox (event_id, event_type, payload, created_at, dead_lettered_at)"
        " VALUES ($1, 'SaleCompleted', $2, now() - interval '1 day', now())",
        dead_id,
        json.dumps({"event_id": str(dead_id), "occurred_at": utcnow().isoformat()}),
    )

    async with edge() as session:
        snap = await read_outbox(session)

    assert snap.depth == 2  # SaleCompleted + PointsEarned của đơn vừa bán
    assert snap.dead_lettered == 1
    assert snap.oldest_unsent_age_seconds < 60  # tuổi của đơn vừa bán, không phải 1 ngày

    await edge_db.execute("UPDATE outbox SET sent_at = now() WHERE dead_lettered_at IS NULL")
    async with edge() as session:
        empty = await read_outbox(session)
    assert (empty.depth, empty.oldest_unsent_age_seconds, empty.dead_lettered) == (0, 0.0, 1)


def _only(**counts: int) -> dict[str, int]:
    return IngestStats(**counts).outcomes()
