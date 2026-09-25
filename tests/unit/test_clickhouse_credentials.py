"""Mật khẩu ClickHouse không bao giờ nằm trong URL.

httpx ghi URL của mỗi request vào log ở mức INFO. Bản đầu của client gửi `?password=…`, và bộ
giám sát luồng in mật khẩu ra `docker logs` ngay lần chạy đầu trên compose (2026-09-25).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from pipeline.clickhouse import ClickHouse
from pipeline.config import ClickHouseConfig
from simulator.audit import ClickHouseTarget, _ch_rows

SECRET = "khong-duoc-lo-ra"


def test_pipeline_client_sends_credentials_in_headers_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="1\n")

    ch = ClickHouse(ClickHouseConfig(url="http://ch:8123", user="dw", password=SECRET))
    ch._client = httpx.Client(transport=httpx.MockTransport(handler))
    ch.rows("SELECT 1", max_threads=1)
    ch.ensure_database()

    assert len(seen) == 2
    for request in seen:
        assert SECRET not in str(request.url)
        assert request.headers["X-ClickHouse-Key"] == SECRET
    assert seen[0].url.params["max_threads"] == "1"  # cài đặt truy vấn vẫn đi bằng query string


def test_audit_client_sends_credentials_in_headers_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="1\n")

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **_: real(transport=httpx.MockTransport(handler))
    )
    target = ClickHouseTarget.parse(f"http://dw:{SECRET}@ch:8123/dw")
    assert asyncio.run(_ch_rows(target, "SELECT 1")) == [["1"]]

    assert SECRET not in str(seen[0].url)
    assert seen[0].headers["X-ClickHouse-Key"] == SECRET
