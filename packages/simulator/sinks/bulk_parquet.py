"""Chế độ `bulk` — lịch sử nhiều tháng ghi THẲNG thành Parquet đúng schema bronze (docs/18 §3).

Cho những câu hỏi mà không đường ghi thật nào trả lời kịp trên một laptop (docs/18 §2): dữ liệu
T2 24 tháng (~256 triệu dòng hàng) trong ClickHouse cho LD-3, và ~50 triệu dòng `point_ledger`
cho phép đo partition pruning. Đi vào ở S5: file nằm trên lake như file S4 ghi ra, rồi chính bộ
nạp thật (`python -m pipeline load`) và dbt thật đưa chúng tới mart.

**Chỉ để đo khối lượng, không làm bằng chứng đúng** (docs/18 §3): không đi qua cửa hàng hay
trung tâm nào. Nhưng hình dạng dữ liệu là thật:

- Lịch ý định từ CÙNG bộ sinh (`plan_store`) với chế độ `edge`/`virtual`: hai đỉnh trong ngày,
  cuối tuần, Zipf theo giá, khách quay lại, khách mới.
- Tiền và điểm từ CÙNG domain của edge (`price_cart`, `resolve_tier`, `points_earned_for`) như
  chế độ `virtual`: không có bộ tính tiền thứ hai để lệch.
- Schema Parquet khai ở ĐÂY (bộ giả lập không được import `pipeline`), và một test hợp đồng
  (`tests/integration/test_bulk_schema.py`) so nó với schema bước trích xuất S4 ghi ra. Lệch
  schema thì LD-3 đang đo một thứ khác với production.

## Hai bước

1. **Sinh theo cửa hàng**, song song nhiều tiến trình (`generate_store`): mỗi cửa hàng ghi mảnh
   Parquet vào thư mục làm việc cục bộ, chia theo THÁNG của `recorded_at` —
   `<work>/<bảng>/<YYYYMM>/<store_id>.parquet`.
2. **Gộp theo tháng** (`publish_month`): mọi mảnh của một (bảng, tháng) → MỘT file trên lake,
   cửa sổ `[đầu tháng, đầu tháng sau)` theo UTC, đúng quy ước tên file của S4
   (`<start>_<end>.parquet` dưới `dt=<ngày đầu cửa sổ>`). Cửa sổ tháng thay vì giờ: 24 × 7 file
   thay cho 120 nghìn — bộ nạp và `loaded_until()` không phụ thuộc độ dài cửa sổ.

`recorded_at` = `occurred_at` + độ trễ đồng bộ vài giây tới một phút: đơn cuối tháng có thể vào
file tháng sau, đúng tình huống bẫy 5 (docs/17 §4).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq

from edge.loyalty.domain.points import EarnRule, points_earned_for
from edge.loyalty.domain.tier import resolve_tier
from edge.pos.domain.pricing import CartLine, price_cart
from shared.config import PricingRules
from shared.seed_data import demo_employees
from simulator.generator import CloseShift, OpenShift, Sale, plan_store
from simulator.sinks.edge_http import payment_split

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import pyarrow.fs as pafs

    from simulator.profile import Profile

__all__ = [
    "INCREMENTAL_TABLES",
    "SCHEMAS",
    "SNAPSHOT_TABLES",
    "StoreStats",
    "generate_store",
    "month_window",
    "months_between",
    "publish_month",
    "uuid7",
]

_TS = pa.timestamp("us", tz="UTC")
_S, _I32, _I64, _D = pa.string(), pa.int32(), pa.int64(), pa.date32()


def _schema(*cols: tuple[str, pa.DataType]) -> pa.Schema:
    return pa.schema([pa.field(n, t) for n, t in cols])


#: Schema bronze — PHẢI trùng từng cột (tên, kiểu, thứ tự) với `pipeline.tables` (test hợp đồng).
#: UUID đi dạng chuỗi như bước trích xuất; bộ nạp `toUUID()` lúc đọc.
SCHEMAS: dict[str, pa.Schema] = {
    "sale": _schema(
        ("sale_id", _S), ("store_id", _S), ("shift_id", _S), ("business_date", _D),
        ("employee_id", _S), ("customer_id", _S), ("occurred_at", _TS), ("recorded_at", _TS),
        ("subtotal", _I64), ("discount_tier", _I64), ("discount_promo", _I64), ("total", _I64),
        ("tendered_amount", _I64), ("change_amount", _I64), ("promotion_id", _S),
        ("authorized_by_employee_id", _S), ("status", _S), ("voided_by_sale_id", _S),
        ("original_sale_id", _S),
    ),
    "sale_line": _schema(
        ("sale_id", _S), ("line_no", _I32), ("product_id", _S), ("quantity", _I32),
        ("unit_price", _I64), ("line_total", _I64), ("original_sale_id", _S),
        ("original_sale_line_no", _I32), ("recorded_at", _TS),
    ),
    "sale_payment": _schema(
        ("sale_id", _S), ("seq", _I32), ("method", _S), ("amount", _I64), ("reference", _S),
        ("recorded_at", _TS),
    ),
    "point_ledger": _schema(
        ("event_id", _S), ("customer_id", _S), ("store_id", _S), ("sale_id", _S),
        ("delta", _I32), ("reason", _S), ("occurred_at", _TS), ("recorded_at", _TS),
    ),
    "shift": _schema(
        ("shift_id", _S), ("store_id", _S), ("business_date", _D),
        ("opened_by_employee_id", _S), ("closed_by_employee_id", _S), ("opened_at", _TS),
        ("closed_at", _TS), ("opening_cash", _I64), ("expected_cash", _I64),
        ("counted_cash", _I64), ("variance", _I64), ("recorded_at", _TS),
    ),
    "customer": _schema(
        ("customer_id", _S), ("joined_at", _TS), ("status", _S), ("merged_into", _S),
        ("recorded_at", _TS),
    ),
    "point_balance": _schema(
        ("customer_id", _S), ("balance", _I32), ("lifetime_earned", _I32), ("tier", _S),
        ("last_event_at", _TS), ("updated_at", _TS),
    ),
}  # fmt: skip

INCREMENTAL_TABLES = tuple(SCHEMAS)
#: Master data: chụp nguyên bảng một lần (dbt lấy `_dt` mới nhất) — chép từ lake thật.
SNAPSHOT_TABLES = ("region", "store", "category", "product", "employee", "tier_rule")

#: Khóa sắp xếp của từng bảng — cùng `ORDER BY` của đặc tả trích xuất (bytes tất định).
SORT_KEYS: dict[str, tuple[str, ...]] = {
    "sale": ("sale_id",),
    "sale_line": ("sale_id", "line_no"),
    "sale_payment": ("sale_id", "seq"),
    "point_ledger": ("event_id",),
    "shift": ("shift_id",),
    "customer": ("customer_id",),
    "point_balance": ("customer_id",),
}

_STAMP = "%Y%m%dT%H%M%SZ"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def uuid7(ms: int, rnd: int) -> str:
    """UUIDv7 mang mốc `ms` (của `occurred_at`, như cửa hàng sinh lúc bán) + 74 bit ngẫu nhiên."""
    value = (
        (ms & (2**48 - 1)) << 80
        | 0x7 << 76
        | ((rnd >> 62) & 0xFFF) << 64
        | 0b10 << 62
        | (rnd & (2**62 - 1))
    )
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def month_window(month: str) -> tuple[datetime, datetime]:
    """`"202409"` → `[2024-09-01, 2024-10-01)` UTC."""
    start = datetime(int(month[:4]), int(month[4:]), 1, tzinfo=UTC)
    end = datetime(start.year + start.month // 12, start.month % 12 + 1, 1, tzinfo=UTC)
    return start, end


def _us(ts: datetime) -> int:
    return (ts - _EPOCH) // timedelta(microseconds=1)


@dataclass(slots=True)
class StoreStats:
    store_id: str
    sales: int = 0
    lines: int = 0
    ledger: int = 0
    customers: int = 0
    shifts: int = 0
    total: int = 0
    months: list[str] = field(default_factory=list)


class _Month:
    """Cột của mọi bảng cho MỘT tháng (theo `recorded_at`) của MỘT cửa hàng."""

    def __init__(self) -> None:
        self.cols: dict[str, dict[str, list[Any]]] = {
            t: {f.name: [] for f in s} for t, s in SCHEMAS.items()
        }

    def add(self, table: str, **row: Any) -> None:
        cols = self.cols[table]
        for name, values in cols.items():
            values.append(row.get(name))

    def write(self, work: Path, month: str, store_id: str) -> None:
        for table, cols in self.cols.items():
            schema = SCHEMAS[table]
            arrays = []
            for f in schema:
                values = cols[f.name]
                if f.type == _TS:
                    arrays.append(pa.array(values, pa.int64()).cast(_TS))
                elif f.type == _D:
                    arrays.append(pa.array(values, pa.int32()).cast(_D))
                else:
                    arrays.append(pa.array(values, f.type))
            path = work / table / month / f"{store_id}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_arrays(arrays, schema=schema), path, compression="zstd")


def generate_store(
    profile: Profile,
    *,
    store_id: str,
    prices: Mapping[str, int],
    seed: int,
    nonce: int,
    start_date: date,
    days: int,
    work: Path,
    utc_offset_minutes: int = 420,
) -> StoreStats:
    """Sinh `days` ngày lịch sử của MỘT cửa hàng vào `work/` (bước 1). Chạy được ở tiến trình con.

    Mọi thứ tất định theo (seed, store_id): cùng lệnh → cùng dữ liệu, kể cả UUID."""
    intents = plan_store(
        profile,
        store_id=store_id,
        catalog=list(prices),
        prices=prices,
        seed=seed,
        nonce=nonce,
        days=days,
        rate=1.0,  # `at` = giây giả lập kể từ giờ mở cửa ngày đầu
        start_date=start_date,
    )
    rng = random.Random(f"{seed}:{store_id}:bulk")  # noqa: S311 — tái lập theo seed
    offset = timedelta(minutes=utc_offset_minutes)
    day_seconds = profile.day_hours * 3600
    pricing, earn = PricingRules(), EarnRule()
    employees = demo_employees(store_id)
    manager, cashiers = employees[0].employee_id, [e.employee_id for e in employees[1:]]

    months: dict[str, _Month] = {}
    stats = StoreStats(store_id)
    customers: dict[str, str] = {}  # ref → customer_id
    lifetime: dict[str, int] = {}
    #: tháng → khách → (tích lũy, occurred_at, recorded_at) của lần đổi số dư cuối trong tháng.
    balance_seen: dict[str, dict[str, tuple[int, int, int]]] = {}

    shift_id = ""
    shift_opened_us = 0
    shift_cashier = ""
    shift_day = start_date
    cash_in_shift = 0

    def clock(at: float) -> tuple[datetime, date]:
        day = int(at // day_seconds)
        business = start_date + timedelta(days=day)
        local = datetime(
            business.year, business.month, business.day, profile.open_hour, tzinfo=UTC
        ) + timedelta(seconds=at - day * day_seconds)
        return local - offset, business

    def recorded(occurred: datetime) -> datetime:
        return occurred + timedelta(seconds=rng.uniform(2.0, 60.0))

    def month_of(ts: datetime) -> _Month:
        key = f"{ts:%Y%m}"
        if key not in months:
            months[key] = _Month()
        return months[key]

    def flush(key: str) -> None:
        month = months.pop(key)
        # Một phiên bản số dư mỗi (tháng, khách) — đúng thứ S4 trích ra: dòng `point_balance` bị
        # UPDATE nhiều lần trong một cửa sổ chỉ xuất hiện MỘT lần, ở trạng thái cuối.
        for cid, (life, last_us, rec_us) in balance_seen.pop(key, {}).items():
            month.add(
                "point_balance",
                customer_id=cid,
                balance=life,  # chỉ có EARN: số dư = tích lũy
                lifetime_earned=life,
                tier=str(resolve_tier(life).tier),
                last_event_at=last_us,
                updated_at=rec_us,
            )
        month.write(work, key, store_id)
        stats.months.append(key)

    def flush_before(ts: datetime) -> None:
        # Mọi tháng cũ hơn tháng TRƯỚC của `ts`: không dòng nào (trễ tối đa 1 phút) còn rơi vào.
        horizon = f"{(ts.replace(day=1) - timedelta(days=1)):%Y%m}"
        for key in sorted(k for k in months if k < horizon):
            flush(key)

    for intent in intents:
        occurred, business = clock(intent.at)
        if isinstance(intent, OpenShift):
            flush_before(occurred)
            shift_id = uuid7(_us(occurred) // 1000, rng.getrandbits(74))
            shift_opened_us, shift_day = _us(occurred), business
            shift_cashier = cashiers[intent.shift_no % len(cashiers)]
            cash_in_shift = 0
            continue
        if isinstance(intent, CloseShift):
            if not shift_id:
                continue
            rec = recorded(occurred)
            expected = profile.opening_cash + cash_in_shift
            month_of(rec).add(
                "shift",
                shift_id=shift_id,
                store_id=store_id,
                business_date=(shift_day - date(1970, 1, 1)).days,
                opened_by_employee_id=shift_cashier,
                closed_by_employee_id=manager,
                opened_at=shift_opened_us,
                closed_at=_us(occurred),
                opening_cash=profile.opening_cash,
                expected_cash=expected,
                counted_cash=expected,
                variance=0,
                recorded_at=_us(rec),
            )
            stats.shifts += 1
            shift_id = ""
            continue
        if not isinstance(intent, Sale) or not shift_id:
            continue

        rec = recorded(occurred)
        m = month_of(rec)
        occurred_us, rec_us = _us(occurred), _us(rec)
        customer_id: str | None = None
        if intent.customer is not None:
            ref = intent.customer
            if ref.new or ref.ref not in customers:
                customer_id = uuid7(occurred_us // 1000, rng.getrandbits(74))
                customers[ref.ref] = customer_id
                m.add(
                    "customer",
                    customer_id=customer_id,
                    joined_at=occurred_us,
                    status="ACTIVE",
                    merged_into=None,
                    recorded_at=rec_us,
                )
                stats.customers += 1
            else:
                customer_id = customers[ref.ref]
        life = lifetime.get(customer_id, 0) if customer_id else 0
        tier_pct = resolve_tier(life).discount_pct if customer_id else 0
        priced = price_cart(
            [CartLine(pid, prices[pid], qty) for pid, qty in intent.lines],
            rules=pricing,
            tier_discount_pct=tier_pct,
        )
        sale_id = uuid7(occurred_us // 1000, rng.getrandbits(74))
        m.add(
            "sale",
            sale_id=sale_id,
            store_id=store_id,
            shift_id=shift_id,
            business_date=(business - date(1970, 1, 1)).days,
            employee_id=shift_cashier,
            customer_id=customer_id,
            occurred_at=occurred_us,
            recorded_at=rec_us,
            subtotal=priced.subtotal,
            discount_tier=priced.discount_tier,
            discount_promo=priced.discount_promo,
            total=priced.total,
            status="COMPLETED",
        )
        for line in priced.lines:
            m.add(
                "sale_line",
                sale_id=sale_id,
                line_no=line.line_no,
                product_id=line.product_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                line_total=line.line_total,
                recorded_at=rec_us,
            )
        for seq, p in enumerate(payment_split(intent.payment, priced.total), start=1):
            m.add(
                "sale_payment",
                sale_id=sale_id,
                seq=seq,
                method=p["method"],
                amount=p["amount"],
                recorded_at=rec_us,
            )
            if p["method"] == "CASH":
                cash_in_shift += p["amount"]
        stats.sales += 1
        stats.lines += len(priced.lines)
        stats.total += priced.total

        points = points_earned_for(priced.total, earn) if customer_id else 0
        if customer_id and points:
            m.add(
                "point_ledger",
                event_id=uuid7(occurred_us // 1000, rng.getrandbits(74)),
                customer_id=customer_id,
                store_id=store_id,
                sale_id=sale_id,
                delta=points,
                reason="EARN",
                occurred_at=occurred_us,
                recorded_at=rec_us,
            )
            stats.ledger += 1
            lifetime[customer_id] = life + points
            balance_seen.setdefault(f"{rec:%Y%m}", {})[customer_id] = (
                life + points,
                occurred_us,
                rec_us,
            )

    for key in sorted(months):
        flush(key)
    return stats


def publish_month(
    fs: pafs.FileSystem,
    *,
    work: Path,
    month: str,
    base: str,
    tables: Sequence[str] = INCREMENTAL_TABLES,
) -> dict[str, int]:
    """Bước 2: gộp mọi mảnh của `month` thành MỘT file mỗi bảng trên lake. Trả số dòng/bảng.

    `base` = `<bucket>/<prefix>` (ví dụ `lake/bronze/bulk-t2`). File đã có thì không ghi lại
    (lake bất biến, như S4) — chạy lại lệnh là an toàn."""
    import pyarrow.compute as pc
    import pyarrow.fs as pafs_mod

    start, end = month_window(month)
    name = f"{start:{_STAMP}}_{end:{_STAMP}}.parquet"
    out: dict[str, int] = {}
    for table in tables:
        key = f"{base}/{table}/dt={start.date().isoformat()}/{name}"
        if fs.get_file_info(key).type == pafs_mod.FileType.File:
            out[table] = -1
            continue
        shards = sorted((work / table / month).glob("*.parquet"))
        parts = [pq.read_table(p, schema=SCHEMAS[table]) for p in shards]
        data = pa.concat_tables(parts) if parts else SCHEMAS[table].empty_table()
        if data.num_rows:
            order = pc.sort_indices(data, sort_keys=[(k, "ascending") for k in SORT_KEYS[table]])
            data = data.take(order)
        fs.create_dir(key.rsplit("/", 1)[0], recursive=True)
        with fs.open_output_stream(key) as sink:
            pq.write_table(data, sink, compression="zstd", row_group_size=50_000)
        out[table] = data.num_rows
    return out


def months_between(start: date, days: int, utc_offset_minutes: int = 420) -> list[str]:
    """Các tháng (theo `recorded_at`, UTC) mà `days` ngày kể từ `start` có thể rơi vào."""
    first = datetime(start.year, start.month, start.day, tzinfo=UTC) - timedelta(
        minutes=utc_offset_minutes
    )
    last = first + timedelta(days=days + 1)
    out: list[str] = []
    cur = first.replace(day=1)
    while cur <= last:
        out.append(f"{cur:%Y%m}")
        cur = month_window(f"{cur:%Y%m}")[1]
    return out
