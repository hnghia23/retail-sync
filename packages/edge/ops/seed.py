"""Nạp `product_cache` cho MỘT cửa hàng từ `crawl_data/` — roadmap tuần 1 ngày 4.

## Vì sao nạp thẳng thay vì đợi đồng bộ thật

Cửa hàng chỉ giữ bản sao ĐỌC-ONLY của master data; nguồn sự thật là trung tâm (docs/05 §2).
Kênh đồng bộ thật (FR-C01: `GET /master-data?since_version=`, kéo delta) CHƯA xây — đó là
việc của tuần 2. Script này nạp thẳng từ `crawl_data/` để `GET /products` và `POST /sales`
chạy được với dữ liệu THẬT ngay trong lúc phát triển, coi như kết quả của một lần đồng bộ
đầu tiên thành công (`synced_version = 1`). Khi FR-C01 hoàn thành, script này bị thay thế
chứ không sống song song với nó — hai đường ghi vào `product_cache` sẽ giẫm chân nhau.

Idempotent: UPSERT theo `product_id`.

`--employees`: nạp thêm nhân viên dựng sẵn (`shared.seed_data.demo_employees`) vào
`employee_cache` để bộ giả lập đăng nhập được (docs/18). Mật khẩu lấy từ biến môi trường
`SEED_EMPLOYEE_PASSWORD`, không bao giờ nằm trong code hay tham số dòng lệnh (lịch sử shell).

Chạy:
    uv run python -m edge.ops.seed
    uv run python -m edge.ops.seed --crawl-data /duong/dan/khac
    SEED_EMPLOYEE_PASSWORD=... uv run python -m edge.ops.seed --employees
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text

from shared.security import hash_password
from shared.seed_data import ProductSeed, demo_employees, parse_products

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


_UPSERT_EMPLOYEE = text(
    """
    INSERT INTO employee_cache (employee_id, name, role, password_hash, is_active, synced_version)
    VALUES (:employee_id, :name, :role, :password_hash, true, 1)
    ON CONFLICT (employee_id) DO UPDATE SET
        name = EXCLUDED.name, role = EXCLUDED.role, password_hash = EXCLUDED.password_hash,
        is_active = true, revoked_at = NULL,
        synced_version = employee_cache.synced_version + 1, synced_at = now()
    """
)


async def seed_employee_cache(session: AsyncSession, *, store_id: str, password: str) -> int:
    """Nhân viên dựng sẵn của cửa hàng này. Chạy lại = đặt lại mật khẩu, không nhân bản."""
    if not password:
        raise ValueError("SEED_EMPLOYEE_PASSWORD rỗng — không tạo tài khoản mật khẩu rỗng")
    employees = demo_employees(store_id)
    await session.execute(
        _UPSERT_EMPLOYEE,
        [
            {
                "employee_id": e.employee_id,
                "name": e.name,
                "role": e.role,
                "password_hash": hash_password(password),
            }
            for e in employees
        ],
    )
    return len(employees)


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
    parser.add_argument("--employees", action="store_true", help="nạp nhân viên dựng sẵn")
    args = parser.parse_args()

    import os

    from edge.settings import get_settings
    from shared.db import make_engine, make_session_factory, transaction

    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        async with transaction(make_session_factory(engine)) as session:
            n = await seed_product_cache(session, args.crawl_data)
            n_emp = (
                await seed_employee_cache(
                    session,
                    store_id=settings.store_id,
                    password=os.environ.get("SEED_EMPLOYEE_PASSWORD", ""),
                )
                if args.employees
                else 0
            )
    finally:
        await engine.dispose()

    print(f"store_id={settings.store_id} product_cache={n} employee_cache={n_emp}")


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    from shared.console import utf8_stdio

    utf8_stdio()
    asyncio.run(_main())
