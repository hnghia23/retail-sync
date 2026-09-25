"""Kiểm tĩnh các file migration — bắt lỗi trước khi `alembic upgrade` chạy trên DB thật."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = sorted((ROOT / "packages").glob("*/migrations/versions/*.py"))


@pytest.mark.parametrize("path", VERSIONS, ids=lambda p: f"{p.parts[-4]}/{p.name}")
def test_revision_id_fits_alembic_version_column(path: Path) -> None:
    """`alembic_version.version_num` là `varchar(32)`. ID dài hơn thì migration chạy hết các
    câu lệnh rồi mới chết ở bước ghi phiên bản — với DDL không nằm trong transaction (như
    `CREATE INDEX CONCURRENTLY`) thì DB bị bỏ lại ở trạng thái nửa vời."""
    match = re.search(r'^revision: str = "([^"]+)"', path.read_text(encoding="utf-8"), re.M)
    assert match, "không tìm thấy dòng `revision: str = ...`"
    assert len(match.group(1)) <= 32, f"{match.group(1)!r} dài {len(match.group(1))} > 32 ký tự"


def test_migrations_were_found() -> None:
    assert len(VERSIONS) >= 8
