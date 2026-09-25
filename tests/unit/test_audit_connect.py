"""Bộ đối soát không chết vì một lần đứt kết nối Postgres — nhưng cũng không che cấu hình sai."""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest

from simulator import audit as audit_module


def test_transport_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def flaky(dsn: str, **_: Any) -> str:
        calls.append(dsn)
        if len(calls) < 3:
            raise ConnectionResetError(
                "[WinError 64] The specified network name is no longer available"
            )
        return "conn"

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("asyncpg.connect", flaky)
    monkeypatch.setattr("asyncio.sleep", no_sleep)
    assert asyncio.run(audit_module._pg_connect("postgresql://x")) == "conn"
    assert len(calls) == 3


def test_auth_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def wrong_password(dsn: str, **_: Any) -> str:
        nonlocal calls
        calls += 1
        raise asyncpg.InvalidPasswordError("password authentication failed")

    monkeypatch.setattr("asyncpg.connect", wrong_password)
    with pytest.raises(asyncpg.InvalidPasswordError):
        asyncio.run(audit_module._pg_connect("postgresql://x"))
    assert calls == 1
