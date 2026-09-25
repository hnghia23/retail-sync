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
    demo_employees,
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


#: Quy tắc hạng mặc định — PHẢI khớp `edge.loyalty.domain.tier.DEFAULT_TIER_RULES`, bộ mà
#: cửa hàng dùng khi chưa kéo được `tier_rule_cache`. Lệch nhau thì cửa hàng offline cho khách
#: một hạng, trung tâm xếp một hạng khác. Không import được qua ranh giới edge↔central, nên
#: `tests/unit/test_loyalty_defaults.py` giữ hai bên khớp nhau.
DEFAULT_TIER_RULES: tuple[tuple[str, int, int], ...] = (
    ("BRONZE", 0, 0),
    ("SILVER", 1_000, 3),
    ("GOLD", 5_000, 5),
    ("PLATINUM", 20_000, 8),
)
#: Khớp `edge.loyalty.domain.points.EarnRule()` mặc định — FR-L03: 10.000đ = 1 điểm.
DEFAULT_VND_PER_POINT = 10_000

# `DO NOTHING`, không `DO UPDATE`: quy tắc là dữ liệu vận hành chỉnh được (CLAUDE.md §Ưu
# tiên số 1). Chạy lại seed không được đè lên ngưỡng mà người vận hành đã sửa.
_SEED_TIER_RULE = text(
    """
    INSERT INTO tier_rule (tier, min_lifetime_points, discount_pct)
    VALUES (:tier, :min_lifetime_points, :discount_pct)
    ON CONFLICT (tier) DO NOTHING
    """
)
_SEED_EARN_RULE = text(
    """
    INSERT INTO earn_rule (rule_id, vnd_per_point, multiplier, valid_from)
    VALUES ('default', :vnd_per_point, 1.0, '2000-01-01T00:00:00Z')
    ON CONFLICT (rule_id) DO NOTHING
    """
)


_UPSERT_EMPLOYEE = text(
    """
    INSERT INTO employee (employee_id, store_id, name, role, password_hash)
    VALUES (:employee_id, :store_id, :name, :role, :password_hash)
    ON CONFLICT (employee_id) DO UPDATE SET
        store_id = EXCLUDED.store_id, name = EXCLUDED.name, role = EXCLUDED.role,
        password_hash = EXCLUDED.password_hash, version = employee.version + 1
    """
)


async def seed_employees(session: AsyncSession, *, store_ids: list[str], password: str) -> int:
    """Nhân viên dựng sẵn — CÙNG danh sách với `edge.ops.seed --employees` (`demo_employees`).

    Nguồn của `dim_employee`: đơn ở cửa hàng mang `employee_id` nào thì trung tâm phải có
    dòng đó, nếu không fact mồ côi khóa. Cửa hàng phải tồn tại trước (`provision_store`).
    """
    from shared.security import hash_password

    if not password:
        raise ValueError("SEED_EMPLOYEE_PASSWORD rỗng — không tạo tài khoản mật khẩu rỗng")
    rows = [
        {
            "employee_id": e.employee_id,
            "store_id": e.store_id,
            "name": e.name,
            "role": e.role,
            "password_hash": hash_password(password),
        }
        for store_id in store_ids
        for e in demo_employees(store_id)
    ]
    if rows:
        await session.execute(_UPSERT_EMPLOYEE, rows)
    return len(rows)


async def seed_loyalty_rules(session: AsyncSession) -> int:
    await session.execute(
        _SEED_TIER_RULE,
        [
            {"tier": t, "min_lifetime_points": m, "discount_pct": d}
            for t, m, d in DEFAULT_TIER_RULES
        ],
    )
    await session.execute(_SEED_EARN_RULE, {"vnd_per_point": DEFAULT_VND_PER_POINT})
    return len(DEFAULT_TIER_RULES)


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
    parser.add_argument(
        "--employees-for",
        default="",
        help="danh sách store_id, cách nhau bằng dấu phẩy — nạp nhân viên dựng sẵn",
    )
    args = parser.parse_args()

    import os

    from central.settings import get_settings
    from shared.db import make_engine, make_session_factory, transaction

    engine = make_engine(get_settings().database_url)
    try:
        async with transaction(make_session_factory(engine)) as session:
            n_regions, n_stores = await seed_regions_and_stores(session, args.crawl_data)
            n_products = await seed_products(session, args.crawl_data)
            n_tiers = await seed_loyalty_rules(session)
            store_ids = [s.strip() for s in args.employees_for.split(",") if s.strip()]
            n_emp = (
                await seed_employees(
                    session,
                    store_ids=store_ids,
                    password=os.environ.get("SEED_EMPLOYEE_PASSWORD", ""),
                )
                if store_ids
                else 0
            )
    finally:
        await engine.dispose()

    print(
        f"region={n_regions} store={n_stores} product={n_products} tier_rule={n_tiers}"
        f" employee={n_emp}"
    )


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    from shared.console import utf8_stdio

    utf8_stdio()
    asyncio.run(_main())
