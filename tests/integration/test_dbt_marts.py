"""S6 — dbt THẬT trên ClickHouse thật: bronze_* → staging → dim_* / fact_* (docs/17 §2, §4).

Bronze được ghi thẳng bằng SQL (giả lập đầu ra của `packages/pipeline`, gồm cả nhật ký nạp) để
dựng đúng những trạng thái khó gặp: đơn đến trễ qua cuối tháng, nạp dở dang giữa hai bảng,
phiên bản cập nhật, khử trùng hỏng. Mỗi test là một cách S6 có thể sai mà không báo lỗi.
"""
# SQL trong file này chỉ ghép hằng do chính test sinh ra.
# ruff: noqa: S608

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pipeline.clickhouse import ClickHouse
from tests.integration.conftest import Dbt

STORE = "store-001"
INCREMENTAL = ("sale", "sale_line", "sale_payment", "point_ledger", "shift", "customer",
               "point_balance")  # fmt: skip
PRICES = {"P1": 33_333, "P2": 10_001, "P3": 1_999}
_seq = itertools.count()


def _ts(value: datetime) -> str:
    return f"toDateTime64('{value:%Y-%m-%d %H:%M:%S.%f}', 6, 'UTC')"


class Bronze:
    def __init__(self, ch: ClickHouse) -> None:
        self._until: dict[str, datetime] = {}
        self.ch = ch

    def _insert(self, table: str, row: dict[str, str], *, dt: str = "2026-09-01") -> None:
        # `_dt` = ngày của `recorded_at` — đúng bất biến mà S4 giữ (phân vùng `dt=` là ngày đầu
        # cửa sổ chứa `recorded_at`) và mà fact tăng dần dựa vào để cắt phân vùng
        # (macros/incremental.sql). Master data không có `recorded_at`: ngày cố định.
        stamp = row.get("recorded_at") or row.get("updated_at")
        day = f"toDate({stamp})" if stamp else f"toDate('{dt}')"
        row = row | {"_dt": day, "_source_file": f"'test/{next(_seq)}'"}
        self.ch.query(
            f"INSERT INTO {table} ({', '.join(row)}) SELECT {', '.join(row.values())}",
            insert_deduplicate=0,
        )

    def masters(self, *, products: dict[str, int] = PRICES) -> None:
        self._insert("bronze_region", {"region_id": "'north'", "name": "'Miền Bắc'"})
        self._insert(
            "bronze_store",
            {"store_id": f"'{STORE}'", "name": "'CH 001'", "region_id": "'north'",
             "status": "'ACTIVE'"},
        )  # fmt: skip
        self._insert("bronze_category", {"category_id": "'c1'", "name": "'Đồ uống'"})
        for pid, price in products.items():
            self._insert(
                "bronze_product",
                {"product_id": f"'{pid}'", "sku": f"'{pid}'", "name": f"'{pid}'",
                 "category_id": "'c1'", "unit_price": str(price), "is_sellable": "true"},
            )  # fmt: skip
        self._insert(
            "bronze_employee",
            {"employee_id": "'e1'", "store_id": f"'{STORE}'", "name": "'Thu ngân'",
             "role": "'cashier'", "status": "'ACTIVE'"},
        )  # fmt: skip

    def customer(self, customer_id: uuid.UUID, *, recorded: datetime) -> None:
        self._insert(
            "bronze_customer",
            {"customer_id": f"toUUID('{customer_id}')", "joined_at": _ts(recorded),
             "status": "'ACTIVE'", "recorded_at": _ts(recorded)},
        )  # fmt: skip

    def sale(
        self,
        *,
        occurred: datetime,
        recorded: datetime,
        lines: list[tuple[str, int]],
        discount_tier: int = 0,
        discount_promo: int = 0,
        split_card: int = 0,
        customer: uuid.UUID | None = None,
        status: str = "COMPLETED",
        sale_id: uuid.UUID | None = None,
        with_lines: bool = True,
        with_payments: bool = True,
        shift_id: uuid.UUID | None = None,
    ) -> tuple[uuid.UUID, int]:
        sale_id = sale_id or uuid.uuid4()
        subtotal = sum(PRICES.get(p, 1_000) * q for p, q in lines)
        total = subtotal - discount_tier - discount_promo
        business_date = (occurred + timedelta(hours=7)).date()
        self._insert(
            "bronze_sale",
            {"sale_id": f"toUUID('{sale_id}')", "store_id": f"'{STORE}'",
             "business_date": f"toDate32('{business_date}')", "employee_id": "'e1'",
             "customer_id": f"toUUID('{customer}')" if customer else "NULL",
             "shift_id": f"toUUID('{shift_id}')" if shift_id else "NULL",
             "occurred_at": _ts(occurred), "recorded_at": _ts(recorded),
             "subtotal": str(subtotal), "discount_tier": str(discount_tier),
             "discount_promo": str(discount_promo), "total": str(total),
             "status": f"'{status}'"},
        )  # fmt: skip
        if with_lines:
            for no, (pid, qty) in enumerate(lines, start=1):
                price = PRICES.get(pid, 1_000)
                self._insert(
                    "bronze_sale_line",
                    {"sale_id": f"toUUID('{sale_id}')", "line_no": str(no),
                     "product_id": f"'{pid}'", "quantity": str(qty),
                     "unit_price": str(price), "line_total": str(price * qty),
                     "recorded_at": _ts(recorded)},
                )  # fmt: skip
        if with_payments:
            self.payments(sale_id, total, split_card=split_card, recorded=recorded)
        return sale_id, total

    def payments(
        self, sale_id: uuid.UUID, total: int, *, split_card: int = 0, recorded: datetime
    ) -> None:
        parts = [("CASH", total - split_card)] + ([("CARD", split_card)] if split_card else [])
        for seq, (method, amount) in enumerate(parts, start=1):
            self._insert(
                "bronze_sale_payment",
                {"sale_id": f"toUUID('{sale_id}')", "seq": str(seq), "method": f"'{method}'",
                 "amount": str(amount), "recorded_at": _ts(recorded)},
            )  # fmt: skip

    def points(self, customer: uuid.UUID, sale_id: uuid.UUID, delta: int, *, at: datetime) -> None:
        self._insert(
            "bronze_point_ledger",
            {"event_id": f"toUUID('{uuid.uuid4()}')", "customer_id": f"toUUID('{customer}')",
             "store_id": f"'{STORE}'", "sale_id": f"toUUID('{sale_id}')", "delta": str(delta),
             "reason": "'EARN'", "occurred_at": _ts(at), "recorded_at": _ts(at)},
        )  # fmt: skip

    def loaded(self, until: datetime, *, tables: tuple[str, ...] = INCREMENTAL) -> None:
        """Nhật ký nạp: các bảng đã nạp LIỀN MẠCH tới `until` — cửa sổ mới bắt đầu đúng ở mép
        cuối của cửa sổ trước, như S4 (lần đầu: phủ 60 ngày trước đó). Fact tăng dần dựa vào
        điều đó để biết phân vùng `_dt` nào có thể chứa dòng của một tháng."""
        for table in tables:
            start = self._until.get(table, until - timedelta(days=60))
            self.ch.query(
                "INSERT INTO bronze_load_log (table_name, path, sha256, rows, window_start,"
                f" window_end) SELECT '{table}', 'log/{next(_seq)}', 'x', 0,"
                f" {_ts(start)}, {_ts(until)}",
                insert_deduplicate=0,
            )
            self._until[table] = until


@pytest.fixture
def bronze(ch_db: Any) -> Any:
    ch = ClickHouse(ch_db)
    try:
        b = Bronze(ch)
        b.masters()
        yield b
    finally:
        ch.close()


def _ok(result: Any) -> str:
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    return str(result.stdout)


def _parts(ch: ClickHouse, table: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for partition, name in ch.rows(
        "SELECT partition, name FROM system.parts WHERE active"
        f" AND database = currentDatabase() AND table = '{table}'"
    ):
        out.setdefault(partition, set()).add(name)
    return out


def _by_sale(ch: ClickHouse) -> dict[str, tuple[int, int, int]]:
    """sale_id → (Σ line_total, Σ net_amount, Σ thanh toán)."""
    lines = {
        r[0]: (int(r[1]), int(r[2]))
        for r in ch.rows(
            "SELECT toString(sale_id), sum(line_total), sum(net_amount) FROM fact_sale_line"
            " GROUP BY sale_id"
        )
    }
    paid = {
        r[0]: int(r[1])
        for r in ch.rows("SELECT toString(sale_id), sum(amount) FROM fact_payment GROUP BY sale_id")
    }
    assert lines.keys() == paid.keys()
    return {k: (*lines[k], paid[k]) for k in lines}


T0 = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)


# ═══════════════ Đúng tới từng đồng + chạy lại ═══════════════


def test_marts_add_up_to_the_dong_and_rerunning_changes_nothing(bronze: Bronze, dbt: Dbt) -> None:
    """Cổng A: Σ net = total = Σ thanh toán cho MỌI đơn, kể cả chiết khấu chia lẻ; AT-07: dbt
    chạy lại không thay phân vùng nào."""
    cust = uuid.uuid4()
    bronze.customer(cust, recorded=T0)
    # Chiết khấu 1 801đ chia cho 3 dòng tỉ lệ 99 999 : 10 001 : 13 993 — không chia hết.
    odd, odd_total = bronze.sale(
        occurred=T0, recorded=T0, lines=[("P1", 3), ("P2", 1), ("P3", 7)],
        discount_tier=1_234, discount_promo=567, customer=cust,
    )  # fmt: skip
    split, split_total = bronze.sale(occurred=T0, recorded=T0, lines=[("P2", 5)], split_card=20_000)
    bronze.points(cust, odd, 123, at=T0)
    bronze.loaded(T0 + timedelta(hours=1))

    _ok(dbt.build())
    ch = bronze.ch
    got = _by_sale(ch)
    assert got[str(odd)] == (123_993, odd_total, odd_total)
    assert got[str(split)] == (50_005, split_total, split_total)
    assert ch.rows("SELECT sum(delta), count() FROM fact_point_event") == [["123", "1"]]

    before = {t: _parts(ch, t) for t in ("fact_sale_line", "fact_payment", "fact_point_event")}
    _ok(dbt.build())
    assert {t: _parts(ch, t) for t in before} == before  # không thay phân vùng nào
    assert _by_sale(ch) == got


# ═══════════════ Bẫy 5 — đến trễ qua cuối tháng ═══════════════


def test_late_sale_recomputes_the_month_it_belongs_to_not_the_current_one(
    bronze: Bronze, dbt: Dbt
) -> None:
    """Cửa hàng offline, đơn 23:30 ngày 31/8 (giờ VN) tới trung tâm ngày 3/9: tháng 8 phải được
    tính lại, tháng 9 để nguyên."""
    aug = datetime(2026, 8, 31, 3, 0, tzinfo=UTC)
    sep = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
    a, _ = bronze.sale(occurred=aug, recorded=aug, lines=[("P1", 1)])
    b, _ = bronze.sale(occurred=sep, recorded=sep, lines=[("P2", 1)])
    bronze.loaded(sep + timedelta(hours=1))
    _ok(dbt.build())
    sep_parts = _parts(bronze.ch, "fact_sale_line")["202609"]

    late_at = datetime(2026, 8, 31, 16, 30, tzinfo=UTC)
    arrived = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    late, _ = bronze.sale(occurred=late_at, recorded=arrived, lines=[("P3", 2)])
    bronze.loaded(arrived + timedelta(hours=1))
    _ok(dbt.build())

    months = {
        r[0]: set(r[1].strip("[]").replace("'", "").split(","))
        for r in bronze.ch.rows(
            "SELECT toString(toYYYYMM(occurred_at)), groupArray(toString(sale_id))"
            " FROM fact_sale_line GROUP BY 1"
        )
    }
    assert months == {"202608": {str(a), str(late)}, "202609": {str(b)}}
    assert _parts(bronze.ch, "fact_sale_line")["202609"] == sep_parts  # tháng 9 không bị đụng
    assert str(late) in _by_sale(bronze.ch)  # và có cả thanh toán


# ═══════════════ Nạp dở dang giữa hai bảng ═══════════════


def test_partially_loaded_window_stays_invisible_until_every_table_catches_up(
    bronze: Bronze, dbt: Dbt
) -> None:
    """Nạp chết sau `sale` + `sale_line`, trước `sale_payment`: nếu fact thấy đơn mà chưa thấy
    thanh toán thì test cổng A đỏ GIẢ và chặn luồng. Phải vô hình cho tới khi đủ."""
    bronze.sale(occurred=T0, recorded=T0, lines=[("P1", 1)])
    bronze.loaded(T0 + timedelta(minutes=30))
    _ok(dbt.build())

    t1 = T0 + timedelta(hours=1)
    pending, total = bronze.sale(occurred=t1, recorded=t1, lines=[("P2", 2)], with_payments=False)
    bronze.loaded(t1 + timedelta(minutes=30), tables=("sale", "sale_line"))
    _ok(dbt.build())
    assert str(pending) not in _by_sale(bronze.ch)

    bronze.payments(pending, total, recorded=t1)
    bronze.loaded(t1 + timedelta(minutes=30))
    _ok(dbt.build())
    assert _by_sale(bronze.ch)[str(pending)] == (20_002, total, total)


# ═══════════════ Phiên bản cập nhật, khử trùng hỏng, master data lệch ═══════════════


def test_updated_sale_keeps_only_its_latest_version(bronze: Bronze, dbt: Dbt) -> None:
    """Bẫy 3 tới tận mart: đơn bị UPDATE có hai phiên bản ở bronze — fact chỉ một, bản mới."""
    sale, total = bronze.sale(occurred=T0, recorded=T0, lines=[("P1", 1), ("P2", 1)])
    later = T0 + timedelta(hours=2)
    bronze.sale(
        occurred=T0, recorded=later, lines=[("P1", 1), ("P2", 1)], status="VOIDED", sale_id=sale
    )
    bronze.loaded(later + timedelta(hours=1))
    _ok(dbt.build())

    assert bronze.ch.rows(
        f"SELECT count(), any(sale_status) FROM fact_sale_line WHERE sale_id = toUUID('{sale}')"
    ) == [["2", "VOIDED"]]
    assert _by_sale(bronze.ch)[str(sale)] == (43_334, total, total)


def test_duplicate_version_in_bronze_fails_the_build(bronze: Bronze, dbt: Dbt) -> None:
    """Khử trùng khi nạp hỏng (bẫy 4) → staging `LIMIT 1 BY` che mất, nên phải đỏ ở bronze."""
    sale, _ = bronze.sale(occurred=T0, recorded=T0, lines=[("P1", 1)])
    bronze.sale(occurred=T0, recorded=T0, lines=[("P1", 1)], sale_id=sale)  # nạp đôi
    bronze.loaded(T0 + timedelta(hours=1))

    result = dbt.build()
    assert result.returncode != 0
    assert "assert_bronze_has_no_duplicate_versions" in result.stdout


def test_unknown_product_becomes_an_inferred_member_and_only_warns(
    bronze: Bronze, dbt: Dbt
) -> None:
    """Trung tâm nhận đơn không cần khóa ngoại tới master data (cửa hàng tự chủ) → mart phải
    chịu được: dim có dòng `is_inferred`, dbt cảnh báo mà không chặn luồng."""
    bronze.sale(occurred=T0, recorded=T0, lines=[("GHOST", 1)])
    bronze.loaded(T0 + timedelta(hours=1))

    out = _ok(dbt.build())
    assert "no_inferred_members_dim_product" in out
    assert bronze.ch.rows("SELECT product_id FROM dim_product WHERE is_inferred = 1") == [["GHOST"]]
    assert bronze.ch.rows("SELECT count() FROM dim_customer WHERE is_inferred = 1") == [
        ["0"]
    ]  # khách vãng lai (NULL) không thành "khách inferred" UUID 0


def test_sale_in_a_still_open_shift_does_not_break_the_build(bronze: Bronze, dbt: Dbt) -> None:
    """Ca chỉ lên trung tâm khi đóng. DAG chạy giữa ngày → đơn trỏ tới ca trung tâm chưa biết.
    Phải dựng được (ca inferred, `is_closed = 0`), không chặn luồng — phát hiện trên compose với
    bộ giả lập `virtual` 20 cửa hàng."""
    shift = uuid.uuid4()
    bronze.sale(occurred=T0, recorded=T0, lines=[("P1", 1)], shift_id=shift)
    bronze.loaded(T0 + timedelta(hours=1))

    _ok(dbt.build())
    assert bronze.ch.rows(
        f"SELECT is_inferred, is_closed, store_id FROM dim_shift WHERE shift_id = toUUID('{shift}')"
    ) == [["1", "0", "store-001"]]
