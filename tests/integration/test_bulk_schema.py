"""Hợp đồng schema của chế độ `bulk` (docs/18 §3, docs/16 §1).

Bộ giả lập không được import `pipeline` (import-linter), nên nó khai schema bronze của riêng
mình. Test này là chỗ DUY NHẤT hai bên gặp nhau: lệch một cột (tên, kiểu, thứ tự) là LD-3 đang
đo một thứ khác với thứ S4 ghi ra ở production, và bộ nạp sẽ đọc sai hoặc chết ở `toUUID()`.

Không cần container: schema S4 ghi ra CHÍNH LÀ `TableSpec.schema()` (bộ trích xuất ghi bằng đúng
schema đó), còn mảnh bulk được đọc lại từ file thật.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from edge.loyalty.domain.points import EarnRule, points_earned_for
from pipeline.tables import INCREMENTAL
from simulator.profile import load_profile
from simulator.sinks.bulk_parquet import (
    INCREMENTAL_TABLES,
    SCHEMAS,
    SNAPSHOT_TABLES,
    generate_store,
    month_window,
    uuid7,
)

PRICES = {f"p{i:03d}": 5_000 + 7_919 * i % 200_000 for i in range(60)}


def test_bulk_covers_every_incremental_table_of_the_extractor() -> None:
    assert set(INCREMENTAL_TABLES) == {spec.name for spec in INCREMENTAL}


@pytest.mark.parametrize("spec", INCREMENTAL, ids=lambda s: s.name)
def test_bulk_schema_equals_extractor_schema(spec: object) -> None:
    from pipeline.tables import TableSpec

    assert isinstance(spec, TableSpec)
    assert SCHEMAS[spec.name].equals(spec.schema()), (
        f"{spec.name}: bulk {SCHEMAS[spec.name]} ≠ S4 {spec.schema()}"
    )


def test_snapshot_tables_match_the_extractor() -> None:
    from pipeline.tables import SNAPSHOT

    assert set(SNAPSHOT_TABLES) == {spec.name for spec in SNAPSHOT}


def _generate(work: Path, *, seed: int = 7) -> None:
    generate_store(
        load_profile("t0"),
        store_id="store-bulk",
        prices=PRICES,
        seed=seed,
        nonce=1,
        start_date=date(2026, 1, 30),  # vắt qua cuối tháng: có đơn ghi nhận ở tháng sau
        days=4,
        work=work,
    )


def _table(root: Path, table: str) -> pa.Table:
    return pa.concat_tables(pq.read_table(p) for p in sorted((root / table).glob("*/*.parquet")))


def test_written_shards_carry_exactly_the_contract_schema(tmp_path: Path) -> None:
    _generate(tmp_path)
    for table, schema in SCHEMAS.items():
        shards = sorted((tmp_path / table).glob("*/*.parquet"))
        assert shards, f"không có mảnh nào cho {table}"
        for shard in shards:
            assert pq.read_schema(shard).equals(schema), f"{shard}: {pq.read_schema(shard)}"


def test_rows_are_internally_consistent(tmp_path: Path) -> None:
    """Dữ liệu chỉ để đo khối lượng, nhưng vẫn phải là dữ liệu mà dbt nhận: mọi bất biến mà
    test dbt kiểm (DI-2, DI-3, điểm theo quy tắc tích) đúng ngay tại nguồn."""
    _generate(tmp_path)

    def read(table: str) -> dict[str, list[Any]]:
        # Bỏ cột timestamp trước khi ra Python: đổi timestamptz sang `datetime` cần tzdata, mà
        # Windows không có sẵn. So thời gian bằng pyarrow.compute ở dưới.
        t = _table(tmp_path, table)
        cols = [f.name for f in t.schema if not pa.types.is_timestamp(f.type)]
        out: dict[str, list[Any]] = t.select(cols).to_pydict()
        return out

    sales, lines, pays, ledger = (
        read("sale"),
        read("sale_line"),
        read("sale_payment"),
        read("point_ledger"),
    )
    total = dict(zip(sales["sale_id"], sales["total"], strict=True))
    discount = {
        sid: t + p
        for sid, t, p in zip(
            sales["sale_id"], sales["discount_tier"], sales["discount_promo"], strict=True
        )
    }
    line_sum: dict[object, int] = {}
    for sid, amount in zip(lines["sale_id"], lines["line_total"], strict=True):
        line_sum[sid] = line_sum.get(sid, 0) + int(amount)
    pay_sum: dict[object, int] = {}
    for sid, amount in zip(pays["sale_id"], pays["amount"], strict=True):
        pay_sum[sid] = pay_sum.get(sid, 0) + int(amount)
    assert len(total) > 100
    for sid, t in total.items():
        assert line_sum[sid] - discount[sid] == t  # DI-2
        assert pay_sum[sid] == t  # DI-3
    for sid, delta in zip(ledger["sale_id"], ledger["delta"], strict=True):
        assert delta == points_earned_for(total[sid], EarnRule())
    # recorded_at luôn sau occurred_at, và đơn cuối tháng có thể sang file tháng sau (bẫy 5).
    occurred = _table(tmp_path, "sale").column("occurred_at")
    recorded = _table(tmp_path, "sale").column("recorded_at")
    assert pc.all(pc.greater(recorded, occurred)).as_py()
    assert {p.name for p in (tmp_path / "sale").iterdir()} == {"202601", "202602"}


def test_same_seed_same_bytes(tmp_path: Path) -> None:
    """Tái lập theo seed (docs/18 §1), kể cả UUID — chạy lại lệnh cho đúng dữ liệu cũ."""
    _generate(tmp_path / "a")
    _generate(tmp_path / "b")

    def digest(root: Path) -> str:
        h = hashlib.sha256()
        for shard in sorted(root.rglob("*.parquet")):
            h.update(shard.read_bytes())
        return h.hexdigest()

    assert digest(tmp_path / "a") == digest(tmp_path / "b")


def test_uuid7_is_time_ordered_and_well_formed() -> None:
    a, b = uuid7(1_700_000_000_000, 2**73), uuid7(1_700_000_000_001, 0)
    assert a < b  # mốc ms đứng đầu → sắp theo thời gian như UUIDv7 thật
    assert a[14] == "7" and a[19] in "89ab"  # version 7, variant RFC 4122


def test_month_window_is_utc_and_rolls_over_the_year() -> None:
    start, end = month_window("202612")
    assert (start.isoformat(), end.isoformat()) == (
        "2026-12-01T00:00:00+00:00",
        "2027-01-01T00:00:00+00:00",
    )
