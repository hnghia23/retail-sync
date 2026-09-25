"""Client đồng bộ + backoff — phần thuần của sync worker, không cần Docker.

Điều quan trọng nhất được kiểm ở đây: MỌI thất bại ở cấp request đều thành
`CentralUnavailableError` — tức lỗi đường truyền, không trừ lượt thử của sự kiện nào. Phần
"không trừ lượt thử" được kiểm trên Postgres thật ở `tests/integration/test_sync_pipeline.py`.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from edge.sync.backoff import next_backoff
from edge.sync.client import CentralUnavailableError, HttpCentralClient

EVENT_ID = uuid.UUID("01935e2a-0000-7000-8000-000000000001")


def _client(handler: object) -> HttpCentralClient:
    return HttpCentralClient(
        base_url="http://central",
        api_key="k",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def test_ok_response_is_parsed_and_key_is_sent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        body = {
            "accepted": [str(EVENT_ID)],
            "rejected": [{"event_id": str(uuid.uuid4()), "reason": "x", "retryable": False}],
        }
        return httpx.Response(200, json=body)

    result = await _client(handler).push([{"event_id": str(EVENT_ID)}])

    assert seen["auth"] == "Bearer k"
    assert result.accepted == [EVENT_ID]
    assert result.rejected[0].retryable is False


async def test_rejection_without_retryable_field_defaults_to_retry() -> None:
    """Trung tâm cũ chưa biết `retryable` → worker phải THỬ LẠI, không vứt sự kiện."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rejected": [{"event_id": str(EVENT_ID), "reason": "x"}]})

    result = await _client(handler).push([])
    assert result.rejected[0].retryable is True


@pytest.mark.parametrize("status", [401, 413, 429, 500, 502, 503])
async def test_non_200_is_a_transport_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"Retry-After": "7"})

    with pytest.raises(CentralUnavailableError) as info:
        await _client(handler).push([])
    assert info.value.status_code == status
    assert info.value.retry_after == 7


async def test_network_failure_is_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("hết giờ", request=request)

    with pytest.raises(CentralUnavailableError):
        await _client(handler).push([])


async def test_unreadable_200_is_not_treated_as_success() -> None:
    """Đoán nhầm "đã gửi" là mất sự kiện; đoán nhầm "chưa gửi" chỉ là gửi lại (idempotent)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>proxy error</html>")

    with pytest.raises(CentralUnavailableError):
        await _client(handler).push([])


@pytest.mark.parametrize("value", ["abc", "-3", None])
async def test_bad_retry_after_is_ignored(value: str | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Retry-After": value} if value is not None else {}
        return httpx.Response(503, headers=headers)

    with pytest.raises(CentralUnavailableError) as info:
        await _client(handler).push([])
    assert info.value.retry_after is None


def test_backoff_is_bounded_and_fully_jittered() -> None:
    """Jitter toàn phần: cận dưới là 0, không phải 90% delay — để N cửa hàng không đồng pha."""
    samples = [next_backoff(20, max_seconds=300) for _ in range(500)]
    assert all(0 <= s <= 300 for s in samples)
    assert min(samples) < 60, "jitter phải trải xuống sát 0"
    assert all(next_backoff(0, max_seconds=300) <= 1 for _ in range(50))
