"""Nạp `product_cache` cho MỘT cửa hàng từ `crawl_data/` — roadmap tuần 1 ngày 4.

## Vì sao nạp thẳng thay vì đợi đồng bộ thật

Cửa hàng chỉ giữ bản sao ĐỌC-ONLY của master data; nguồn sự thật là trung tâm (docs/05 §2).
Kênh đồng bộ thật (FR-C01: `GET /master-data?since_version=`, kéo delta) CHƯA xây — đó là
việc của tuần 2. Script này nạp thẳng từ `crawl_data/` để `GET /products` và `POST /sales`
chạy được với dữ liệu THẬT ngay trong lúc phát triển, coi như kết quả của một lần đồng bộ
đầu tiên thành công (`synced_version = 1`). Khi FR-C01 hoàn thành, script này bị thay thế
chứ không sống song song với nó — hai đường ghi vào `product_cache` sẽ giẫm chân nhau.

Idempotent: UPSERT theo `product_id`.

Chạy:
    uv run python -m edge.ops.seed
    uv run python -m edge.ops.seed --crawl-data /duong/dan/khac
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text

from shared.seed_data import ProductSeed, parse_products

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: `packages/edge/ops/seed.py` -> lùi 3 cấp là gốc repo, nơi có `crawl_data/`.
_DEFAULT_CRAWL_DATA = Path(__file__).resolve().parents[3] / "crawl_data"

_UPSERT_PRODUCT = text(
    """
    INSERT INTO product_cache (
        product_id, sku, barcode, name, unit_price, synced_version, synced_at
    )
    VALUES (:product_id, :sku, :barcode, :name, :unit_price, 1, now())
    ON CONFLICT (product_id) DO UPDATE SET
        sku = EXCLUDED.sku, barcode = EXCLUDED.barcode,
        name = EXCLUDED.name, unit_price = EXCLUDED.unit_price,
        synced_version = product_cache.synced_version + 1, synced_at = now()
    """
)


async def seed_product_cache(session: AsyncSession, crawl_data: Path) -> int:
    products = parse_products(crawl_data / "products" / "product_details.csv")
    if products:
        await session.execute(_UPSERT_PRODUCT, _params(products))
    return len(products)


def _params(products: list[ProductSeed]) -> list[dict[str, object]]:
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

    from edge.settings import get_settings
    from shared.db import make_engine, make_session_factory, transaction

    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        async with transaction(make_session_factory(engine)) as session:
            n = await seed_product_cache(session, args.crawl_data)
    finally:
        await engine.dispose()

    print(f"store_id={settings.store_id} product_cache={n}")


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    asyncio.run(_main())
