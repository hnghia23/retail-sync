"""ClickHouse qua HTTP (cổng 8123) bằng httpx — không thêm client lib cho một giao thức đơn giản."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pipeline.config import ClickHouseConfig

__all__ = ["DDL", "ClickHouse", "ClickHouseError", "quote"]

DDL = Path(__file__).parent / "ddl" / "bronze.sql"


class ClickHouseError(RuntimeError):
    pass


def quote(value: str) -> str:
    """Chuỗi hằng an toàn cho SQL ClickHouse (chỉ dùng với giá trị do pipeline tự sinh)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


class ClickHouse:
    def __init__(self, cfg: ClickHouseConfig, *, timeout: float = 300.0) -> None:
        self.cfg = cfg
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    @property
    def _auth(self) -> dict[str, str]:
        """Thông tin đăng nhập đi bằng HEADER, không bằng query string.

        Bản đầu gửi `?user=…&password=…`: httpx ghi URL mỗi request vào log ở mức INFO, và bộ
        giám sát luồng (log INFO, đo mỗi 30 giây) in mật khẩu ClickHouse ra `docker logs` ngay
        lần chạy đầu trên compose (2026-09-25). Header không nằm trong URL nên không vào log."""
        return {"X-ClickHouse-User": self.cfg.user, "X-ClickHouse-Key": self.cfg.password}

    def query(self, sql: str, **settings: object) -> str:
        params = {"database": self.cfg.database} | {k: str(v) for k, v in settings.items()}
        r = self._client.post(self.cfg.url, params=params, headers=self._auth, content=sql.encode())
        if r.status_code != 200:
            # Thông điệp của ClickHouse không chứa mật khẩu.
            raise ClickHouseError(r.text.strip()[:2000])
        return r.text.strip()

    def rows(self, sql: str, **settings: object) -> list[list[str]]:
        out = self.query(f"{sql} FORMAT TSV", **settings)
        return [line.split("\t") for line in out.splitlines()] if out else []

    def ensure_database(self) -> None:
        self._client.post(
            self.cfg.url,
            headers=self._auth,
            content=f"CREATE DATABASE IF NOT EXISTS {self.cfg.database}".encode(),
        ).raise_for_status()

    def apply_ddl(self, path: Path = DDL) -> int:
        """`CREATE TABLE IF NOT EXISTS` — chạy lại vô hại. Đổi cấu trúc bảng cần migration riêng."""
        items = statements(path.read_text(encoding="utf-8"))
        for s in items:
            self.query(s)
        return len(items)
