"""Dữ liệu master TỔNG HỢP đủ lớn cho LD-2/LD-4 trên máy sạch (CI, VPS) — không cần `crawl_data/`.

Bộ `tests/fixtures/crawl_data/` chỉ có ~32 cửa hàng: đủ cho `tests/scenarios/`, nhưng LD-2 (10 → 200
cửa hàng ảo) và LD-4 (200 kết nối) cấp khóa cho 200 cửa hàng CÓ THẬT trong master data. Thiếu thì
`provision_store --from-master 200` chỉ cấp được số đang có, và bậc "200" âm thầm đo ít hơn.

Script chép bộ fixture sang thư mục ra rồi thêm tỉnh `tinh-mau-c` (đã có trong `links.csv`, chưa có
danh sách cửa hàng) với N cửa hàng. Sản phẩm giữ nguyên bộ fixture — tải LD-2/LD-4 đo đường đồng bộ,
không phụ thuộc cỡ danh mục.

    uv run python infra/loadtest/synth_master.py --out runs/synth-master --stores 260
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "crawl_data"
SLUG, PROVINCE = "tinh-mau-c", "Tỉnh Mẫu C"


def build(out: Path, stores: int) -> Path:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(FIXTURE, out)
    links = (out / "store_info" / "links.csv").read_text(encoding="utf-8-sig")
    if f",{SLUG}," not in links:
        raise SystemExit(f"{FIXTURE}/store_info/links.csv không còn tỉnh {SLUG}")
    target = out / "store_info" / "provinces" / f"{SLUG}.csv"
    with target.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["district", "shop_name", "address"])
        for i in range(1, stores + 1):
            district = f"Quận {i % 12 + 1}"
            w.writerow(
                [district, f"Cửa hàng mẫu C{i:03d}", f"{i} Đường Mẫu, {district}, {PROVINCE}"]
            )
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / "synth-master")
    parser.add_argument("--stores", type=int, default=260, help="số cửa hàng thêm vào tỉnh mẫu C")
    args = parser.parse_args(argv)
    out = build(args.out.resolve(), args.stores)
    print(f"{out}: fixture + {args.stores} cửa hàng ({SLUG})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
