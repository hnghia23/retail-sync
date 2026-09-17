"""Đọc dữ liệu crawl thật (`crawl_data/`) thành bản ghi seed thuần — roadmap tuần 1 ngày 4.

Thuần: chỉ đọc CSV, không chạm DB. Cả `central.ops.seed` lẫn `edge.ops.seed` dùng chung
module này (đặt ở `shared` vì đây là tiện ích tổng quát, giống `shared.db`/`shared.security`
— không phải nghiệp vụ riêng của module nào).

`crawl_data/` là tài sản giữ lại từ v1 (CLAUDE.md): dữ liệu sản phẩm và cửa hàng THẬT của
một chuỗi bán lẻ. Dữ liệu crawl có lỗi thật — không phải lỗi parse — và được xử lý tường
minh thay vì bỏ qua âm thầm:

1. **68 nhóm `code` trùng giữa các sản phẩm KHÔNG liên quan** (ví dụ mã `"DCC"` vừa là một
   cây đèn chống cận vừa là một miếng mặt nạ dưỡng da). Không bỏ sản phẩm: bản ghi thứ n
   trùng mã được thêm hậu tố `-n` vào `product_id`; cột `sku` vẫn giữ nguyên mã gốc (cột
   này không có ràng buộc UNIQUE trong schema).

2. **9 mã dạng số 8–14 chữ số (trông như barcode/EAN) bị gán trùng cho hai sản phẩm khác
   nhau** — ví dụ mã `8935246918845` vừa là "Đắc nhân tâm" vừa là "Giáo trình Hán ngữ 4".
   Barcode PHẢI xác định đúng một sản phẩm (docs/13: `GET /products?barcode=` cho thu
   ngân quét hàng); gán cùng một barcode cho hai `product_id` sẽ vi phạm
   `UNIQUE INDEX ... WHERE barcode IS NOT NULL` và làm việc quét mã vạch trả sai hàng. Chỉ
   BẢN GHI ĐẦU TIÊN của một mã trùng được gán `barcode`; các bản ghi trùng sau đó có
   `barcode = None` — thà thiếu mã vạch còn hơn mã vạch trỏ nhầm sản phẩm.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

_PRICE_DIGITS = re.compile(r"[^\d]")
_LOOKS_LIKE_BARCODE = re.compile(r"^\d{8,14}$")


@dataclass(frozen=True, slots=True)
class ProductSeed:
    product_id: str
    sku: str
    barcode: str | None
    name: str
    unit_price: int


@dataclass(frozen=True, slots=True)
class RegionSeed:
    region_id: str
    name: str


@dataclass(frozen=True, slots=True)
class StoreSeed:
    store_id: str
    name: str
    address: str
    city: str
    region_id: str


class SeedDataError(ValueError):
    """Dữ liệu nguồn không đọc được — dừng seed thay vì nạp dữ liệu hỏng."""


def _parse_price(raw: str) -> int:
    """`"210,000 đ"` → `210000`. Không dùng float — tiền là số nguyên đồng (CLAUDE.md)."""
    digits = _PRICE_DIGITS.sub("", raw)
    if not digits:
        raise SeedDataError(f"Không đọc được giá tiền: {raw!r}")
    return int(digits)


def parse_products(csv_path: Path) -> list[ProductSeed]:
    """Đọc `products/product_details.csv` (bộ đầy đủ hơn `product.csv` — 5556 so với 459
    dòng — nên dùng bộ này làm nguồn duy nhất thay vì hợp hai file).

    Xem docstring module về hai loại lỗi dữ liệu được xử lý tường minh ở đây: mã trùng
    giữa sản phẩm khác nhau, và mã dạng số bị gán trùng.
    """
    seen: dict[str, int] = {}
    result: list[ProductSeed] = []

    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = row["code"].strip()
            name = " ".join(row["name"].split())  # gộp khoảng trắng thừa từ HTML crawl
            if not code or not name:
                continue

            occurrence = seen.get(code, 0) + 1
            seen[code] = occurrence
            product_id = code if occurrence == 1 else f"{code}-{occurrence}"

            is_barcode_shaped = bool(_LOOKS_LIKE_BARCODE.fullmatch(code))
            barcode = code if (is_barcode_shaped and occurrence == 1) else None

            result.append(
                ProductSeed(
                    product_id=product_id,
                    sku=code,
                    barcode=barcode,
                    name=name,
                    unit_price=_parse_price(row["price"]),
                )
            )
    return result


def parse_regions_and_stores(store_info_dir: Path) -> tuple[list[RegionSeed], list[StoreSeed]]:
    """Đọc `store_info/links.csv` (danh sách tỉnh) + `store_info/provinces/*.csv` (cửa
    hàng từng tỉnh) — 57 tỉnh, ~3500 cửa hàng thật.

    `store_id` sinh từ `<slug tỉnh>-NNNN` (1-based, đệm 4 số theo thứ tự dòng trong file
    nguồn) — ổn định qua nhiều lần chạy vì crawl_data là dữ liệu tĩnh, không đổi thứ tự.
    """
    links_path = store_info_dir / "links.csv"
    if not links_path.exists():
        raise SeedDataError(f"Không thấy {links_path}")

    regions: list[RegionSeed] = []
    stores: list[StoreSeed] = []

    with links_path.open(encoding="utf-8-sig", newline="") as f:
        provinces = list(csv.DictReader(f))

    for province in provinces:
        slug = province["slug"].strip()
        name = province["province"].strip()
        if not slug or not name:
            continue
        regions.append(RegionSeed(region_id=slug, name=name))

        province_path = store_info_dir / "provinces" / f"{slug}.csv"
        if not province_path.exists():
            # Tỉnh có trong links.csv nhưng chưa crawl được danh sách cửa hàng — vùng vẫn
            # tạo được (báo cáo theo khu vực không phụ thuộc việc có cửa hàng hay chưa).
            continue

        with province_path.open(encoding="utf-8-sig", newline="") as f:
            for i, row in enumerate(csv.DictReader(f), start=1):
                shop_name = row["shop_name"].strip()
                address = row["address"].strip()
                if not shop_name:
                    continue
                stores.append(
                    StoreSeed(
                        store_id=f"{slug}-{i:04d}",
                        name=shop_name,
                        address=address,
                        city=name,
                        region_id=slug,
                    )
                )

    return regions, stores
