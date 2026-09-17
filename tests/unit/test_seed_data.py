"""Parser dữ liệu crawl — `shared/seed_data.py`.

Dùng CSV tổng hợp nhỏ (không phải file `crawl_data/` thật): unit test không cần Docker và
không nên phụ thuộc dữ liệu ngoài, vốn có thể đổi hoặc vắng mặt trên máy CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.seed_data import RegionSeed, SeedDataError, parse_products, parse_regions_and_stores


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8-sig", newline="")
    return path


# ═══════════════════════ parse_products ═══════════════════════


def test_parses_price_and_fields(tmp_path: Path) -> None:
    csv_path = _write(
        tmp_path / "products.csv",
        'name,price,code\nNuoc mam,"150,000 đ",SKU-001\n',
    )
    products = parse_products(csv_path)
    assert len(products) == 1
    p = products[0]
    assert p.product_id == "SKU-001"
    assert p.sku == "SKU-001"
    assert p.unit_price == 150_000
    assert p.name == "Nuoc mam"


def test_digit_code_becomes_barcode(tmp_path: Path) -> None:
    csv_path = _write(
        tmp_path / "products.csv",
        'name,price,code\nGao,"100,000 đ",8935246918845\n',
    )
    products = parse_products(csv_path)
    assert products[0].barcode == "8935246918845"
    assert products[0].sku == "8935246918845"


def test_short_or_long_digit_code_is_not_a_barcode(tmp_path: Path) -> None:
    """Barcode thật (EAN) dài 8–14 số. Mã 3 chữ số hay 20 chữ số không phải barcode."""
    csv_path = _write(
        tmp_path / "products.csv",
        'name,price,code\nA,"1000 đ",123\nB,"1000 đ",12345678901234567890\n',
    )
    products = parse_products(csv_path)
    assert products[0].barcode is None
    assert products[1].barcode is None


def test_alnum_code_has_no_barcode(tmp_path: Path) -> None:
    csv_path = _write(
        tmp_path / "products.csv",
        'name,price,code\nCombo,"260,000 đ",MNLS-ho\n',
    )
    assert parse_products(csv_path)[0].barcode is None


def test_whitespace_in_name_is_collapsed(tmp_path: Path) -> None:
    csv_path = _write(
        tmp_path / "products.csv",
        'name,price,code\n"Nuoc   mam   Phu   Quoc","50,000 đ",SKU-002\n',
    )
    assert parse_products(csv_path)[0].name == "Nuoc mam Phu Quoc"


def test_rows_missing_code_or_name_are_skipped(tmp_path: Path) -> None:
    csv_path = _write(
        tmp_path / "products.csv",
        'name,price,code\n,"1000 đ",SKU-001\nA,"1000 đ",\nB,"1000 đ",SKU-002\n',
    )
    products = parse_products(csv_path)
    assert [p.product_id for p in products] == ["SKU-002"]


def test_unparseable_price_raises(tmp_path: Path) -> None:
    csv_path = _write(tmp_path / "products.csv", "name,price,code\nA,Lien he,SKU-001\n")
    with pytest.raises(SeedDataError):
        parse_products(csv_path)


# ═══════════════════════ Mã trùng — dữ liệu crawl có lỗi thật ═══════════════════════


def test_duplicate_code_gets_disambiguated_product_id(tmp_path: Path) -> None:
    """68 nhóm mã trùng có thật trong dữ liệu crawl: hai sản phẩm KHÔNG liên quan dùng
    chung một `code`. Không bỏ sản phẩm nào — cả hai phải còn lại với `product_id` khác nhau."""
    csv_path = _write(
        tmp_path / "products.csv",
        'name,price,code\nMi tron trung,"21,000 đ",MTTC\nMi tron thap cam,"31,000 đ",MTTC\n',
    )
    products = parse_products(csv_path)
    assert [p.product_id for p in products] == ["MTTC", "MTTC-2"]
    # `sku` giữ nguyên mã gốc ở CẢ HAI — cột này không UNIQUE trong schema.
    assert [p.sku for p in products] == ["MTTC", "MTTC"]


def test_duplicate_digit_code_gives_barcode_to_first_occurrence_only(tmp_path: Path) -> None:
    """⚠️ Bất biến quan trọng nhất của module này.

    Dữ liệu crawl thật có 9 mã dạng số bị gán trùng cho hai sản phẩm khác nhau (ví dụ
    "8935246918845" vừa là "Đắc nhân tâm" vừa là "Giáo trình Hán ngữ 4"). Gán barcode cho
    cả hai sẽ vi phạm UNIQUE INDEX và khiến quét mã vạch trả sai sản phẩm — chỉ bản ghi
    ĐẦU TIÊN được giữ barcode.
    """
    csv_path = _write(
        tmp_path / "products.csv",
        (
            "name,price,code\n"
            'Dac nhan tam,"90,000 đ",8935246918845\n'
            'Giao trinh Han ngu 4,"95,000 đ",8935246918845\n'
        ),
    )
    products = parse_products(csv_path)
    assert products[0].product_id == "8935246918845"
    assert products[0].barcode == "8935246918845"
    assert products[1].product_id == "8935246918845-2"
    assert products[1].barcode is None
    # `sku` vẫn giữ mã gốc ở cả hai — chỉ `barcode` bị thu hồi ở bản ghi thứ hai.
    assert products[1].sku == "8935246918845"


def test_three_way_duplicate_disambiguates_every_occurrence(tmp_path: Path) -> None:
    csv_path = _write(
        tmp_path / "products.csv",
        'name,price,code\nA,"1 đ",X\nB,"1 đ",X\nC,"1 đ",X\n',
    )
    products = parse_products(csv_path)
    assert [p.product_id for p in products] == ["X", "X-2", "X-3"]


# ═══════════════════════ parse_regions_and_stores ═══════════════════════


def test_parses_regions_and_stores(tmp_path: Path) -> None:
    root = tmp_path / "store_info"
    root.mkdir()
    _write(root / "links.csv", "province,slug,link\nTP. Ha Noi,ha-noi,https://x\n")
    (root / "provinces").mkdir()
    _write(
        root / "provinces" / "ha-noi.csv",
        'district,shop_name,address\nQ. Ba Dinh,"WM+ 1",Dia chi 1\nQ. Cau Giay,"WM+ 2",Dia chi 2\n',
    )

    regions, stores = parse_regions_and_stores(root)
    assert regions == [RegionSeed(region_id="ha-noi", name="TP. Ha Noi")]
    assert [s.store_id for s in stores] == ["ha-noi-0001", "ha-noi-0002"]
    assert stores[0].name == "WM+ 1"
    assert stores[0].city == "TP. Ha Noi"
    assert stores[0].region_id == "ha-noi"


def test_region_without_province_file_still_created(tmp_path: Path) -> None:
    """Tỉnh có trong links.csv nhưng chưa crawl được danh sách cửa hàng — vùng vẫn tạo được."""
    root = tmp_path / "store_info"
    root.mkdir()
    _write(root / "links.csv", "province,slug,link\nTinh chua crawl,tcc,https://x\n")
    (root / "provinces").mkdir()

    regions, stores = parse_regions_and_stores(root)
    assert len(regions) == 1
    assert stores == []


def test_missing_links_csv_raises(tmp_path: Path) -> None:
    root = tmp_path / "store_info"
    root.mkdir()
    with pytest.raises(SeedDataError):
        parse_regions_and_stores(root)


def test_store_row_without_shop_name_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "store_info"
    root.mkdir()
    _write(root / "links.csv", "province,slug,link\nA,a,https://x\n")
    (root / "provinces").mkdir()
    _write(
        root / "provinces" / "a.csv", 'district,shop_name,address\nD,,Dia chi\nD,"WM+",Dia chi\n'
    )

    _, stores = parse_regions_and_stores(root)
    assert len(stores) == 1
    assert stores[0].store_id == "a-0002"  # thứ tự dòng gốc vẫn giữ nguyên, kể cả dòng bị bỏ
