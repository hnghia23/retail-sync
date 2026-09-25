"""Gửi lô sự kiện lên `POST /api/v1/events` của trung tâm — docs/13 §2.

Một ranh giới rõ ràng giữa hai loại thất bại, vì worker xử lý chúng NGƯỢC nhau:

  - `CentralUnavailableError` — cả request không thành (mạng, timeout, 401, 413, 429, 5xx).
    Là lỗi của ĐƯỜNG TRUYỀN: worker lùi lại cả vòng lặp, KHÔNG trừ lượt thử của sự kiện nào.
  - Trả về `IngestResult` — trung tâm đã xét TỪNG sự kiện. Lỗi (nếu có) là của sự kiện đó.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from shared.events import IngestResult

EVENTS_PATH = "/api/v1/events"


class CentralUnavailableError(Exception):
    """Trung tâm không nhận được lô này — thử lại cả lô sau.

    `retry_after`: giây, lấy từ header `Retry-After` khi trung tâm chủ động từ chối vì quá
    tải (docs/08 §3.2). Worker tôn trọng giá trị này thay vì tự tính backoff.
    """

    def __init__(
        self, message: str, *, retry_after: float | None = None, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


class CentralClient(Protocol):
    async def push(self, envelopes: Sequence[Mapping[str, Any]]) -> IngestResult: ...


class HttpCentralClient:
    """`transport` chỉ để test: trỏ thẳng vào ASGI app của trung tâm, không qua mạng thật."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def push(self, envelopes: Sequence[Mapping[str, Any]]) -> IngestResult:
        try:
            response = await self._client.post(EVENTS_PATH, json=list(envelopes))
        except httpx.HTTPError as exc:
            raise CentralUnavailableError(f"transport: {type(exc).__name__}: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            raise CentralUnavailableError(
                f"http {response.status_code}",
                retry_after=_retry_after(response.headers.get("Retry-After")),
                status_code=response.status_code,
            )
        try:
            return IngestResult.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            # Phản hồi 200 mà không đọc được thì KHÔNG được đoán là thành công — coi như lô
            # chưa gửi. Gửi lại thì idempotency lo; đoán nhầm "đã gửi" thì mất sự kiện.
            raise CentralUnavailableError("phản hồi không hợp lệ từ trung tâm") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after(value: str | None) -> float | None:
    """Chỉ hiểu dạng số giây. Dạng ngày HTTP (RFC 9110) hợp lệ nhưng trung tâm không dùng."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
