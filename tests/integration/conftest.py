"""Fixture cho test tích hợp — Postgres thật qua testcontainers.

Vì sao Postgres THẬT chứ không sqlite/mock: phần lớn tính đúng đắn của thiết kế này nằm
trong CHECK constraint, constraint trigger hoãn, partial index và bảng phân vùng. Mock đi
là mock mất đúng thứ cần kiểm.

Container dùng chung cho cả session (`scope="session"`) nhưng mỗi test có schema sạch
riêng — khởi động Postgres mất ~3s, chạy migration mất ~0.3s.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from edge.sync.client import HttpCentralClient
from pipeline.clickhouse import ClickHouse as PipelineClickHouse
from pipeline.config import ClickHouseConfig, LakeConfig, PipelineConfig
from pipeline.lake import Lake
from pipeline.run import RunReport, run_once
from shared.db import make_engine, make_session_factory, transaction

ROOT = Path(__file__).resolve().parents[2]

# Ảnh phải khớp infra/compose.yaml: `uuidv7()` là hàm native của PG 18, không có ở PG 17.
POSTGRES_IMAGE = "postgres:18"
# Cùng tag với infra/compose.yaml. Hành vi khử trùng khi insert (docs/17 §4 bẫy 4) được kiểm
# trên ĐÚNG image này — đổi tag ở compose thì test chạy lại trên bản mới, không tin changelog.
CLICKHOUSE_IMAGE = "clickhouse/clickhouse-server:24-alpine"


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """DSN của một Postgres 18 dùng chung cho cả phiên test."""
    testcontainers = pytest.importorskip("testcontainers.postgres")

    with testcontainers.PostgresContainer(POSTGRES_IMAGE, driver=None) as pg:
        yield (
            f"postgresql://{pg.username}:{pg.password}"
            f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
        )


class ClickHouse:
    """Gọi ClickHouse qua HTTP (cổng 8123) bằng httpx — không thêm client lib chỉ để test."""

    def __init__(self, url: str) -> None:
        self.url = url

    def query(self, sql: str, **settings: object) -> str:
        import httpx

        params = {"user": "test", "password": "test"} | {k: str(v) for k, v in settings.items()}
        response = httpx.post(self.url, params=params, content=sql.encode(), timeout=60)
        if response.status_code != 200:
            raise RuntimeError(response.text)
        return response.text.strip()

    def count(self, table: str) -> int:
        # Tên bảng luôn là hằng trong code test, không bao giờ từ dữ liệu ngoài.
        return int(self.query(f"SELECT count() FROM {table}"))  # noqa: S608


def _wait_until(condition: Any, *, timeout: float, what: str) -> Any:
    """Chờ tới khi `condition()` trả giá trị khác None, có hạn chót — không phải retry may rủi.

    Docker Desktop trên máy này báo cổng NAT trễ vài trăm ms sau khi container chạy (cùng
    nguyên nhân với race của Ryuk, xem CLAUDE.md).
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value is not None:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"quá {timeout}s chờ {what}")
        time.sleep(0.25)


# Cùng image với infra/compose.yaml — Docker Hub `minio/minio` đã không còn (2026-09-24).
MINIO_IMAGE = "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z"
MINIO_USER = "minio-test"
MINIO_PASSWORD = "minio-test-secret"


@pytest.fixture(scope="session")
def docker_network() -> Iterator[Any]:
    """Mạng chung cho ClickHouse + MinIO: ClickHouse đọc lake qua `http://minio:9000` bằng
    `s3()`, đúng như trong compose — không qua cổng NAT của máy chủ."""
    network_mod = pytest.importorskip("testcontainers.core.network")
    with network_mod.Network() as net:
        yield net


def _mapped_port(container: Any, port: int) -> int:
    def lookup() -> int | None:
        try:
            return int(container.get_exposed_port(port))
        except ConnectionError:
            return None

    mapped: int = _wait_until(lookup, timeout=30, what=f"cổng {port}")
    return mapped


@pytest.fixture(scope="session")
def clickhouse(docker_network: Any) -> Iterator[ClickHouse]:
    import httpx

    core = pytest.importorskip("testcontainers.core.container")
    container = (
        core.DockerContainer(CLICKHOUSE_IMAGE)
        .with_env("CLICKHOUSE_USER", "test")
        .with_env("CLICKHOUSE_PASSWORD", "test")
        .with_exposed_ports(8123)
        .with_network(docker_network)
        .with_network_aliases("clickhouse")
    )
    with container:
        url = f"http://{container.get_container_host_ip()}:{_mapped_port(container, 8123)}/"

        def ping() -> bool | None:
            try:
                return True if httpx.get(url + "ping", timeout=2).text.strip() == "Ok." else None
            except httpx.HTTPError:
                return None

        _wait_until(ping, timeout=60, what="ClickHouse sẵn sàng")
        yield ClickHouse(url)


@dataclass(frozen=True)
class Minio:
    #: `host:port` nhìn từ tiến trình pytest.
    endpoint: str
    #: URL nhìn từ ClickHouse (cùng mạng Docker).
    url_for_clickhouse: str = "http://minio:9000"
    access_key: str = MINIO_USER
    secret_key: str = MINIO_PASSWORD


@pytest.fixture(scope="session")
def minio(docker_network: Any) -> Iterator[Minio]:
    import httpx

    core = pytest.importorskip("testcontainers.core.container")
    container = (
        core.DockerContainer(MINIO_IMAGE)
        .with_env("MINIO_ROOT_USER", MINIO_USER)
        .with_env("MINIO_ROOT_PASSWORD", MINIO_PASSWORD)
        .with_command("server /data")
        .with_exposed_ports(9000)
        .with_network(docker_network)
        .with_network_aliases("minio")
    )
    with container:
        endpoint = f"{container.get_container_host_ip()}:{_mapped_port(container, 9000)}"

        def live() -> bool | None:
            try:
                r = httpx.get(f"http://{endpoint}/minio/health/live", timeout=2)
                return True if r.status_code == 200 else None
            except httpx.HTTPError:
                return None

        _wait_until(live, timeout=60, what="MinIO sẵn sàng")
        yield Minio(endpoint=endpoint)


@pytest.fixture
def ch_db(clickhouse: ClickHouse) -> Iterator[Any]:
    """Một database ClickHouse sạch, đã áp DDL bronze của `pipeline` — mỗi test một cái."""
    from pipeline.clickhouse import ClickHouse as PipelineClickHouse
    from pipeline.config import ClickHouseConfig

    name = f"dw_{uuid.uuid4().hex[:8]}"
    url = clickhouse.url.rstrip("/")
    cfg = ClickHouseConfig(url=url, user="test", password="test", database=name)
    ch = PipelineClickHouse(cfg)
    ch.ensure_database()
    ch.apply_ddl()
    ch.close()
    yield cfg
    clickhouse.query(f"DROP DATABASE IF EXISTS {name}")


DBT_PROJECT = ROOT / "data_platform" / "dbt"
DBT_REQUIREMENTS = ROOT / "data_platform" / "requirements-dbt.txt"


@dataclass(frozen=True)
class Dbt:
    """dbt THẬT (uvx, venv riêng — không cài dbt vào môi trường của repo) trỏ vào `ch_db`."""

    env: dict[str, str]
    workdir: Path

    def build(self, *args: str) -> Any:
        import subprocess

        cmd = [
            "uvx", "--python", "3.12", "--with-requirements", str(DBT_REQUIREMENTS),
            "--from", "dbt-core", "dbt", "build",
            "--project-dir", str(DBT_PROJECT), "--profiles-dir", str(DBT_PROJECT),
            "--target-path", str(self.workdir / "target"), "--log-path", str(self.workdir / "logs"),
            *args,
        ]  # fmt: skip
        return subprocess.run(  # noqa: S603 — lệnh cố định, không có dữ liệu ngoài
            cmd, env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=900
        )


@pytest.fixture
def dbt(ch_db: Any, tmp_path: Path) -> Dbt:
    from urllib.parse import urlsplit

    url = urlsplit(ch_db.url)
    env = os.environ | {
        "CLICKHOUSE_HOST": url.hostname or "localhost",
        "CLICKHOUSE_PORT": str(url.port or 8123),
        "CLICKHOUSE_USER": ch_db.user,
        "CLICKHOUSE_PASSWORD": ch_db.password,
        "CLICKHOUSE_DB": ch_db.database,
        "PYTHONUTF8": "1",
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    return Dbt(env=env, workdir=tmp_path)


def _run_migrations(alembic_ini: Path, dsn: str, database: str) -> None:
    """Chạy `alembic upgrade head`. **Gọi qua `asyncio.to_thread`, không gọi trực tiếp.**

    `migrations/env.py` tự mở event loop bằng `asyncio.run()` — đó là cách Alembic async
    hoạt động và là cách nó chạy thật ở production. Gọi từ trong một coroutine của pytest
    sẽ nổ "cannot be called from a running event loop", nên fixture đẩy hàm này sang thread
    khác để nó có loop riêng.
    """
    from alembic import command
    from alembic.config import Config

    url = dsn.rsplit("/", 1)[0] + f"/{database}"
    cfg = Config(str(alembic_ini))
    # env.py cố ý đọc DATABASE_URL từ môi trường thay vì từ alembic.ini — secret không bao
    # giờ nằm trong file cấu hình (docs/09-v1-postmortem.md O1).
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url.replace("postgresql://", "postgresql+asyncpg://")
    try:
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


async def _fresh_database(dsn: str, name: str) -> str:
    import asyncpg

    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    return dsn.rsplit("/", 1)[0] + f"/{name}"


@pytest.fixture
async def edge_db(postgres_dsn: str, request: pytest.FixtureRequest) -> AsyncIterator[object]:
    """Kết nối tới một DB cửa hàng sạch, đã chạy migration."""
    import asyncpg

    name = f"edge_{abs(hash(request.node.nodeid)) % 10**8}"
    url = await _fresh_database(postgres_dsn, name)
    await asyncio.to_thread(
        _run_migrations, ROOT / "packages" / "edge" / "alembic.ini", postgres_dsn, name
    )

    conn = await asyncpg.connect(url)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def central_db(postgres_dsn: str, request: pytest.FixtureRequest) -> AsyncIterator[object]:
    """Kết nối tới một DB trung tâm sạch, đã chạy migration."""
    import asyncpg

    name = f"central_{abs(hash(request.node.nodeid)) % 10**8}"
    url = await _fresh_database(postgres_dsn, name)
    await asyncio.to_thread(
        _run_migrations, ROOT / "packages" / "central" / "alembic.ini", postgres_dsn, name
    )

    conn = await asyncpg.connect(url)
    try:
        yield conn
    finally:
        await conn.close()


STORE_ID = "store-001"


@pytest.fixture
async def http_client(
    edge_db: Any, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    """Dựng `edge.main.app` thật, trỏ vào DB test đã migrate, gọi qua HTTP trong tiến trình.

    Dùng chung cho `test_auth_http.py` (JSON API) và `test_web_ui.py` (UI HTML) — cả hai
    đều cần đúng một thứ: FastAPI app thật, nối vào DB test, gọi qua ASGI transport thay
    vì mock. `get_settings()` (và các `get_*` khác) dùng `lru_cache` — phải `cache_clear()`
    trước khi set biến môi trường mới, nếu không app dùng cấu hình của lần import đầu tiên
    trong tiến trình pytest.
    """
    import httpx

    dbname = await edge_db.fetchval("SELECT current_database()")
    base = postgres_dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")

    monkeypatch.setenv("DATABASE_URL", f"{base}/{dbname}")
    monkeypatch.setenv("STORE_ID", STORE_ID)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-http-integration")

    from edge import main as edge_main
    from edge import settings as edge_settings

    for cached in (
        edge_settings.get_settings,
        edge_settings.get_jwt_settings,
        edge_settings.get_pricing_rules,
        edge_settings.get_return_rules,
        edge_settings.get_otel_settings,
        edge_settings.get_pii_settings,
    ):
        cached.cache_clear()

    app = edge_main.create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://edge-under-test"
        ) as client:
            yield client

    for cached in (
        edge_settings.get_settings,
        edge_settings.get_jwt_settings,
        edge_settings.get_pricing_rules,
        edge_settings.get_return_rules,
        edge_settings.get_otel_settings,
        edge_settings.get_pii_settings,
    ):
        cached.cache_clear()


# ═══════════ Trung tâm thật + DB cửa hàng qua SQLAlchemy — dùng chung nhiều file ═══════════
#
# Chuyển từ test_sync_pipeline.py (2026-09-23) để test bộ giả lập đầu-cuối dùng lại được.

CENTRAL_STORE_ID = "store-001"
CENTRAL_OTHER_STORE_ID = "store-002"
CENTRAL_CUSTOMER_ID = uuid.UUID("01935abc-0000-7000-8000-000000000001")


@dataclass
class Central:
    db: Any  # asyncpg — để KIỂM CHỨNG, không phải đường code thật đi
    app: Any
    keys: dict[str, str]

    def client(self, store_id: str = CENTRAL_STORE_ID, **kwargs: Any) -> HttpCentralClient:
        return HttpCentralClient(
            base_url="http://central-under-test",
            api_key=self.keys[store_id],
            timeout_seconds=10,
            transport=kwargs.get("transport") or httpx.ASGITransport(app=self.app),
        )


@pytest.fixture
async def central(
    central_db: Any, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Central]:
    """Central API thật, trỏ vào DB trung tâm test, có 2 cửa hàng đã cấp khóa + quy tắc hạng."""
    from central import main as central_main
    from central import settings as central_settings
    from central.ops.provision_store import provision_store
    from central.ops.seed import seed_loyalty_rules

    dbname = await central_db.fetchval("SELECT current_database()")
    base = postgres_dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
    url = f"{base}/{dbname}"
    monkeypatch.setenv("DATABASE_URL", url)

    # Cấp khóa bằng CHÍNH lệnh ops mà người vận hành chạy — không chèn thẳng vào bảng.
    engine = make_engine(url)
    try:
        async with transaction(make_session_factory(engine)) as session:
            keys = {
                sid: await provision_store(session, store_id=sid, name=sid)
                for sid in (CENTRAL_STORE_ID, CENTRAL_OTHER_STORE_ID)
            }
            await seed_loyalty_rules(session)
    finally:
        await engine.dispose()
    await central_db.execute(
        "INSERT INTO customer (customer_id, phone_hash) VALUES ($1, 'hash-1')",
        CENTRAL_CUSTOMER_ID,
    )

    caches = (
        central_settings.get_settings,
        central_settings.get_return_rules,
        central_settings.get_otel_settings,
    )
    for cached in caches:
        cached.cache_clear()
    app = central_main.create_app()
    async with app.router.lifespan_context(app):
        yield Central(db=central_db, app=app, keys=keys)
    for cached in caches:
        cached.cache_clear()


@pytest.fixture
async def edge(edge_db: Any, postgres_dsn: str) -> AsyncIterator[Any]:
    dbname = await edge_db.fetchval("SELECT current_database()")
    base = postgres_dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
    engine = make_engine(f"{base}/{dbname}")
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


# ═══════ Pipeline S4/S5 trên PG + MinIO + ClickHouse (test_pipeline, test_flow_monitor) ═══════


@dataclass
class Env:
    cfg: PipelineConfig
    lake: Lake
    ch: PipelineClickHouse
    db: Any  # asyncpg — DB trung tâm

    async def run(self) -> RunReport:
        return await run_once(self.cfg, self.lake, self.ch)

    def count(self, table: str) -> int:
        return int(self.ch.query(f"SELECT count() FROM {table}"))  # noqa: S608 — tên bảng của test


@pytest.fixture
async def env(
    central_db: Any, postgres_dsn: str, minio: Minio, ch_db: ClickHouseConfig
) -> AsyncIterator[Env]:
    """Cửa sổ trích xuất 1 giây và độ trễ an toàn 0, để một lượt test không phải chờ một giờ."""
    dbname = await central_db.fetchval("SELECT current_database()")
    lake_cfg = LakeConfig(
        endpoint=minio.endpoint,
        access_key=minio.access_key,
        secret_key=minio.secret_key,
        url_for_clickhouse=minio.url_for_clickhouse,
        bucket=f"lake-{uuid.uuid4().hex[:8]}",
    )
    cfg = PipelineConfig(
        pg_dsn=postgres_dsn.rsplit("/", 1)[0] + f"/{dbname}",
        lake=lake_cfg,
        clickhouse=ch_db,
        window_seconds=1,
        safety_lag_seconds=0,
    )
    lake = Lake(lake_cfg)
    lake.ensure_bucket()
    ch = PipelineClickHouse(ch_db)
    try:
        yield Env(cfg=cfg, lake=lake, ch=ch, db=central_db)
    finally:
        ch.close()
