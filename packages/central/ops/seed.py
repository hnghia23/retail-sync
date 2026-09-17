"""Nạp master data trung tâm từ `crawl_data/` — roadmap tuần 1 ngày 4.

Trung tâm sở hữu master data (docs/05 §2); đây là nguồn sự thật mà `edge.ops.seed` mô
phỏng lại (xem docstring ở đó về vì sao cửa hàng cũng nạp thẳng ở giai đoạn này thay vì
đợi kênh đồng bộ FR-C01).

Mọi lệnh là UPSERT — chạy lại không nhân đôi, và cập nhật được nếu `crawl_data/` thay đổi.

Chạy:
    uv run python -m central.ops.seed
    uv run python -m central.ops.seed --crawl-data /duong/dan/khac
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text

from shared.seed_data import (
    ProductSeed,
    RegionSeed,
    StoreSeed,
    parse_products,
    parse_regions_and_stores,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: `packages/central/ops/seed.py` -> lùi 3 cấp là gốc repo, nơi có `crawl_data/`.
_DEFAULT_CRAWL_DATA = Path(__file__).resolve().parents[3] / "crawl_data"

_UPSERT_REGION = text(
    """
    INSERT INTO region (region_id, name) VALUES (:region_id, :name)
    ON CONFLICT (region_id) DO UPDATE SET name = EXCLUDED.name
    """
)

_UPSERT_STORE = text(
    """
    INSERT INTO store (store_id, name, address, city, region_id)
    VALUES (:store_id, :name, :address, :city, :region_id)
    ON CONFLICT (store_id) DO UPDATE SET
        name = EXCLUDED.name, address = EXCLUDED.address,
        city = EXCLUDED.city, region_id = EXCLUDED.region_id
    """
)

_UPSERT_PRODUCT = text(
    """
    INSERT INTO product (product_id, sku, barcode, name, unit_price)
    VALUES (:product_id, :sku, :barcode, :name, :unit_price)
    ON CONFLICT (product_id) DO UPDATE SET
        sku = EXCLUDED.sku, barcode = EXCLUDED.barcode,
        name = EXCLUDED.name, unit_price = EXCLUDED.unit_price,
        -- `version` tăng ở MỖI lần seed, kể cả khi giá trị không đổi. Chấp nhận được: đây
        -- là script nạp dữ liệu ban đầu chạy vài lần lúc phát triển, không phải luồng cập
        -- nhật giá sản xuất — nơi version phải chỉ tăng khi giá trị thực sự đổi.
        version = product.version + 1
    """
)


async def seed_regions_and_stores(session: AsyncSession, crawl_data: Path) -> tuple[int, int]:
    regions, stores = parse_regions_and_stores(crawl_data / "store_info")

    if regions:
        await session.execute(_UPSERT_REGION, _region_params(regions))
    if stores:
        await session.execute(_UPSERT_STORE, _store_params(stores))
    return len(regions), len(stores)


async def seed_products(session: AsyncSession, crawl_data: Path) -> int:
    products = parse_products(crawl_data / "products" / "product_details.csv")
    if products:
        await session.execute(_UPSERT_PRODUCT, _product_params(products))
    return len(products)


def _region_params(regions: list[RegionSeed]) -> list[dict[str, object]]:
    return [{"region_id": r.region_id, "name": r.name} for r in regions]


def _store_params(stores: list[StoreSeed]) -> list[dict[str, object]]:
    return [
        {
            "store_id": s.store_id,
            "name": s.name,
            "address": s.address,
            "city": s.city,
            "region_id": s.region_id,
        }
        for s in stores
    ]


def _product_params(products: list[ProductSeed]) -> list[dict[str, object]]:
    return [
        {
            "product_id": p.product_id,
            "sku": p.sku,
            "barcode": p.barcode,
            "name": p.name,
            "unit_price": p.unit_price,
        }
        for p in products
    ]


async def _main() -> None:  # pragma: no cover — điểm vào CLI
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crawl-data", type=Path, default=_DEFAULT_CRAWL_DATA)
    args = parser.parse_args()

    from central.settings import get_settings
    from shared.db import make_engine, make_session_factory, transaction

    engine = make_engine(get_settings().database_url)
    try:
        async with transaction(make_session_factory(engine)) as session:
            n_regions, n_stores = await seed_regions_and_stores(session, args.crawl_data)
            n_products = await seed_products(session, args.crawl_data)
    finally:
        await engine.dispose()

    print(f"region={n_regions} store={n_stores} product={n_products}")


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    asyncio.run(_main())
