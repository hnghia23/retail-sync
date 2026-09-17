"""Fixture dùng chung.

Phân tầng test (docs/16-test-plan.md §1):
  unit/        — logic thuần, KHÔNG Docker, chạy < 1s
  integration/ — testcontainers: Postgres/Redis/ClickHouse thật
  scenarios/   — end-to-end trên compose stack
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tự gắn marker theo thư mục — đỡ phải nhớ decorate từng test."""
    for item in items:
        path = str(item.path)
        if "integration" in path:
            item.add_marker(pytest.mark.integration)
        elif "scenarios" in path:
            item.add_marker(pytest.mark.scenario)
