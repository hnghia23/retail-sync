"""Kịch bản end-to-end trên COMPOSE THẬT, nguồn dữ liệu là bộ giả lập (docs/16 §1, §2).

Điều kiện đạt chung: sau kịch bản, `simulator audit` trả `CONVERGED` trong thời hạn hội tụ.

**Opt-in** (`RETAIL_SYNC_SCENARIOS=1`, `make test-scenarios`): các test này đổi trạng thái
compose đang chạy — AT-02 TẮT Central API, mọi test mở/đóng ca ở cửa hàng thật. Chạy lẫn vào
`uv run pytest` thường thì sẽ phá bất cứ thứ gì đang chạy trên compose (một lần chạy bộ giả lập,
một test ngâm).

Cần: `--profile edge --profile central --profile data up` (+ `edge-multi` không bắt buộc),
nhân viên dựng sẵn (`make seed-employees`, mật khẩu ở `SEED_EMPLOYEE_PASSWORD`), và cho AT-04
file khóa cửa hàng ảo `runs/virtual-keys.env` (`make provision-virtual`). Secret đọc từ
`infra/.env`, không in ra.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
STORE = "store-001"

# Cùng bộ test chạy được trên stack dev (mặc định) hay stack CI / project compose riêng
# (`infra/bootstrap.py --project … --env-file …`), không sửa code.
PROJECT = os.environ.get("RETAIL_SYNC_COMPOSE_PROJECT", "retail-sync")
ENV_FILE = Path(os.environ.get("RETAIL_SYNC_ENV_FILE", ROOT / "infra" / ".env"))
VIRTUAL_KEYS = Path(os.environ.get("RETAIL_SYNC_VIRTUAL_KEYS", ROOT / "runs" / "virtual-keys.env"))
_REAL_CRAWL = ROOT / "crawl_data"
#: `crawl_data/` thật KHÔNG nằm trong git; clone sạch (CI) dùng bộ tổng hợp cùng định dạng.
CRAWL_DATA = Path(
    os.environ.get("RETAIL_SYNC_CRAWL_DATA")
    or (
        _REAL_CRAWL
        if (_REAL_CRAWL / "products" / "product_details.csv").exists()
        else ROOT / "tests" / "fixtures" / "crawl_data"
    )
)


def container(service: str) -> str:
    """Tên container compose của một service trong project đang thử."""
    return f"{PROJECT}-{service}-1"


def _dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


@dataclass(frozen=True)
class Stack:
    env: dict[str, str]
    password: str
    edge_url: str = "http://localhost:8001"
    central_url: str = "http://localhost:8000"

    @property
    def edge_dsn(self) -> str:
        e = self.env
        return (
            f"postgresql://{e.get('EDGE_DB_USER', 'edge_app')}:{e['EDGE_DB_PASSWORD']}"
            "@localhost:5433/edge_store_001"
        )

    @property
    def central_dsn(self) -> str:
        e = self.env
        return (
            f"postgresql://{e.get('CENTRAL_DB_USER', 'central_app')}:{e['CENTRAL_DB_PASSWORD']}"
            "@localhost:5434/central"
        )

    @property
    def store_key(self) -> str:
        return self.env["CENTRAL_API_KEY"]

    def docker(self, *args: str) -> None:
        subprocess.run(["docker", *args], check=True, capture_output=True, timeout=120)  # noqa: S603, S607


def _up(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    if os.environ.get("RETAIL_SYNC_SCENARIOS") != "1":
        pytest.skip("kịch bản compose là opt-in: RETAIL_SYNC_SCENARIOS=1 (make test-scenarios)")
    env = _dotenv(ENV_FILE)
    # `infra/bootstrap.py` giữ mật khẩu trong file env; biến môi trường vẫn đè được.
    password = os.environ.get("SEED_EMPLOYEE_PASSWORD") or env.get("SEED_EMPLOYEE_PASSWORD", "")
    if not password:
        pytest.fail("thiếu SEED_EMPLOYEE_PASSWORD (mật khẩu nhân viên dựng sẵn)")
    s = Stack(env=env, password=password)
    for url in (s.edge_url, s.central_url):
        if not _up(url):
            pytest.fail(
                f"{url} không trả lời — compose chưa bật (uv run python infra/bootstrap.py)"
            )
    yield s
    # Kịch bản nào tắt trung tâm mà chết giữa chừng thì vẫn bật lại cho người sau.
    subprocess.run(  # noqa: S603 — tên container dựng từ hằng của test
        ["docker", "start", container("central-api")],  # noqa: S607
        capture_output=True,
        timeout=120,
        check=False,
    )
