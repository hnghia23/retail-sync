"""Seed script chạy trên Postgres thật — `central.ops.seed`, `edge.ops.seed`.

Dùng CSV tổng hợp nhỏ trong `tmp_path`, KHÔNG dùng `crawl_data/` thật: test tích hợp cần
nhanh và không phụ thuộc dữ liệu ngoài repo có thể đổi. Bộ dữ liệu thật đã được xác nhận
riêng (chạy tay `parse_products`/`parse_regions_and_stores` trên `crawl_data/` — 5556 sản
phẩm, `product_id`/`barcode` duy nhất; 3515 cửa hàng, `store_id` duy nhất).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from central.ops.seed import seed_products as central_seed_products
from central.ops.seed import seed_regions_and_stores
from edge.ops.seed import seed_product_cache


def _make_crawl_data(root: Path, *, product_rows: str, store_rows: str | None = None) -> Path:
    products_dir = root / "products"
    products_dir.mkdir(parents=True)
    (products_dir / "product_details.csv").write_text(
        "name,price,code\n" + product_rows, encoding="utf-8-sig", newline=""
    )

    store_info = root / "store_info"
    provinces = store_info / "provinces"
    provinces.mkdir(parents=True)
    (store_info / "links.csv").write_text(
        "province,slug,link\nTP. Ha Noi,ha-noi,https://x\n", encoding="utf-8-sig", newline=""
    )
    (provinces / "ha-noi.csv").write_text(
        "district,shop_name,address\n" + (store_rows or 'Q. Ba Dinh,"WM+ 1",Dia chi 1\n'),
        encoding="utf-8-sig",
        newline="",
    )
    return root


# ═══════════════════════ central.ops.seed ═══════════════════════


async def test_seed_products_inserts_into_central_product(
    central_db: Any, tmp_path: Path, postgres_dsn: str
) -> None:
    crawl_data = _make_crawl_data(
        tmp_path, product_rows='Nuoc mam,"150,000 đ",SKU-001\nGao,"100,000 đ",8935246918845\n'
    )

    from shared.db import make_session_factory, transaction

    factory = make_session_factory(await _engine_for(central_db, postgres_dsn))
    async with transaction(factory) as session:
        n = await central_seed_products(session, crawl_data)
    assert n == 2

    rows = await central_db.fetch(
        "SELECT product_id, unit_price, barcode FROM product ORDER BY product_id"
    )
    assert len(rows) == 2
    barcodes = {r["barcode"] for r in rows}
    assert "8935246918845" in barcodes


async def test_seed_products_is_idempotent(
    central_db: Any, tmp_path: Path, postgres_dsn: str
) -> None:
    """Chạy hai lần: số dòng không đổi, `version` tăng lên (cập nhật, không nhân đôi)."""
    crawl_data = _make_crawl_data(tmp_path, product_rows='Nuoc mam,"150,000 đ",SKU-001\n')

    from shared.db import make_session_factory, transaction

    factory = make_session_factory(await _engine_for(central_db, postgres_dsn))
    async with transaction(factory) as session:
        await central_seed_products(session, crawl_data)
    async with transaction(factory) as session:
        await central_seed_products(session, crawl_data)

    assert await central_db.fetchval("SELECT count(*) FROM product") == 1
    # Lần đầu là INSERT (version mặc định = 1), lần hai đi vào nhánh ON CONFLICT (+1).
    assert await central_db.fetchval("SELECT version FROM product") == 2


async def test_seed_products_updates_changed_price(
    central_db: Any, tmp_path: Path, postgres_dsn: str
) -> None:
    """Giá crawl đổi ở lần seed sau — UPSERT phải phản ánh giá mới, không giữ giá cũ."""
    from shared.db import make_session_factory, transaction

    factory = make_session_factory(await _engine_for(central_db, postgres_dsn))

    crawl_v1 = _make_crawl_data(tmp_path / "v1", product_rows='Nuoc mam,"150,000 đ",SKU-001\n')
    async with transaction(factory) as session:
        await central_seed_products(session, crawl_v1)

    crawl_v2 = _make_crawl_data(tmp_path / "v2", product_rows='Nuoc mam,"180,000 đ",SKU-001\n')
    async with transaction(factory) as session:
        await central_seed_products(session, crawl_v2)

    assert await central_db.fetchval("SELECT unit_price FROM product") == 180_000


async def test_seed_regions_and_stores(central_db: Any, tmp_path: Path, postgres_dsn: str) -> None:
    crawl_data = _make_crawl_data(
        tmp_path,
        product_rows='A,"1 đ",X\n',
        store_rows='Q. Ba Dinh,"WM+ 1",Dia chi 1\nQ. Cau Giay,"WM+ 2",Dia chi 2\n',
    )

    from shared.db import make_session_factory, transaction

    factory = make_session_factory(await _engine_for(central_db, postgres_dsn))
    async with transaction(factory) as session:
        n_regions, n_stores = await seed_regions_and_stores(session, crawl_data)
    assert (n_regions, n_stores) == (1, 2)

    assert await central_db.fetchval("SELECT count(*) FROM region") == 1
    stores = await central_db.fetch("SELECT store_id, region_id FROM store ORDER BY store_id")
    assert [s["store_id"] for s in stores] == ["ha-noi-0001", "ha-noi-0002"]
    assert all(s["region_id"] == "ha-noi" for s in stores)


# ═══════════════════════ edge.ops.seed ═══════════════════════


async def test_seed_product_cache_sets_initial_sync_version(
    edge_db: Any, tmp_path: Path, postgres_dsn: str
) -> None:
    crawl_data = _make_crawl_data(tmp_path, product_rows='Nuoc mam,"150,000 đ",SKU-001\n')

    from shared.db import make_session_factory, transaction

    factory = make_session_factory(await _engine_for(edge_db, postgres_dsn))
    async with transaction(factory) as session:
        n = await seed_product_cache(session, crawl_data)
    assert n == 1

    row = await edge_db.fetchrow("SELECT synced_version, is_sellable FROM product_cache")
    assert row["synced_version"] == 1
    assert row["is_sellable"] is True  # mặc định DDL — sản phẩm seed bán được ngay


async def test_seed_product_cache_is_queryable_by_router_adapter(
    edge_db: Any, tmp_path: Path, postgres_dsn: str
) -> None:
    """Xác nhận dữ liệu seed thật sự dùng được cho `PostgresProductCatalog` (đường POS đi qua)."""
    crawl_data = _make_crawl_data(tmp_path, product_rows='Nuoc mam,"150,000 đ",8935246918845\n')

    from edge.pos.adapters.postgres import PostgresProductCatalog
    from shared.db import make_session_factory, transaction

    factory = make_session_factory(await _engine_for(edge_db, postgres_dsn))
    async with transaction(factory) as session:
        await seed_product_cache(session, crawl_data)

    async with factory() as session:
        product = await PostgresProductCatalog(session).by_barcode("8935246918845")
    assert product is not None
    assert product["unit_price"] == 150_000


async def _engine_for(db: Any, postgres_dsn: str) -> Any:
    """Dựng engine SQLAlchemy trỏ vào cùng DB test mà connection asyncpg `db` đang dùng.

    Cùng mẫu với `tests/integration/test_place_sale.py::edge_stack` — connection kiểm
    chứng và đường code thật phải tách nhau để không đọc nhầm dữ liệu chưa commit.
    """
    from shared.db import make_engine

    dbname = await db.fetchval("SELECT current_database()")
    base = postgres_dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
    return make_engine(f"{base}/{dbname}")
