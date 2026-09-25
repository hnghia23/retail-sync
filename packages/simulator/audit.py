"""Bộ đối soát — so đáp án (L0) với Postgres cửa hàng (L1), Postgres trung tâm (L2), bronze
trong ClickHouse (L3) và mart của dbt (L4).

docs/17 §5, docs/18 §6. Ba kết quả:
  - `CONVERGED`  mọi tầng khớp đáp án;
  - `CONVERGING` tầng sau còn THIẾU so với tầng trước nhưng không có gì thừa hay sai, và phần
                 thiếu vẫn đang nằm trong outbox chờ gửi — bình thường khi luồng đang chảy;
  - `DIVERGED`   có gì thừa, có số sai, mất dữ liệu (thiếu mà outbox không còn gì để gửi),
                 hoặc có sự kiện vào dead-letter. Đây là SỰ CỐ.

So theo từng `sale_id`, không chỉ theo tổng: hai lỗi bù nhau (mất một đơn, nhân đôi một đơn
cùng giá) vẫn khớp tổng nhưng không khớp từng đơn. Phạm vi là các CA của lần chạy, nên dữ liệu
khác trong cùng DB (lần chạy trước, bán tay qua UI) không làm nhiễu kết quả.

Đọc thẳng Postgres: đây là quyền của người kiểm tra trong môi trường test. Ở production trung
tâm không đọc được DB cửa hàng (docs/17 §5), tín hiệu thay thế là chỉ số outbox cửa hàng tự báo.

Chế độ `virtual`: cửa hàng ảo không có Postgres, nên "L1" là chính trạng thái nó ghi vào manifest
(mọi thứ nó dựng ra đều đã "commit", outbox còn lại ở `StoreRecord.outbox`). L0→L1 khi đó khớp
theo định nghĩa; phần có nghĩa là L1→L2 trở đi — đúng những chặng chế độ này đi qua.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncpg
import httpx

if TYPE_CHECKING:
    from collections.abc import Iterable

    from simulator.manifest import Manifest

__all__ = ["AuditReport", "ClickHouseTarget", "Finding", "audit", "audit_until_settled"]

CONVERGED, CONVERGING, DIVERGED = "CONVERGED", "CONVERGING", "DIVERGED"


@dataclass(frozen=True, slots=True)
class Finding:
    level: str  # CONVERGING | DIVERGED
    store_id: str
    layer: str  # "L0→L1" | "L1→L2" | "L2→L3" | "L3→L4"
    what: str
    ref: str = ""


@dataclass(slots=True)
class AuditReport:
    status: str
    findings: list[Finding] = field(default_factory=list)
    #: store_id → tầng → business_date → tổng. Hàng của bảng docs/17 §5.
    summary: dict[str, dict[str, dict[str, dict[str, int]]]] = field(default_factory=dict)
    outbox: dict[str, dict[str, int]] = field(default_factory=dict)
    #: store_id → tầng → {newest, freshness_seconds, behind_seconds} — docs/17 §3, §6.
    freshness: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "findings": [_asdict(f) for f in self.findings],
            "summary": self.summary,
            "outbox": self.outbox,
            "freshness": self.freshness,
        }


def _asdict(f: Finding) -> dict[str, str]:
    return {
        "level": f.level,
        "store_id": f.store_id,
        "layer": f.layer,
        "what": f.what,
        "ref": f.ref,
    }


@dataclass(frozen=True, slots=True)
class ClickHouseTarget:
    """Bronze để đối soát L3. Đọc qua HTTP — bộ giả lập không import `pipeline` (ranh giới)."""

    url: str
    user: str
    password: str
    database: str = "dw"

    @classmethod
    def parse(cls, url: str) -> ClickHouseTarget:
        """`http://user:mật-khẩu@host:8123/dw`."""
        from urllib.parse import unquote, urlsplit

        parts = urlsplit(url)
        return cls(
            url=f"{parts.scheme}://{parts.hostname}:{parts.port or 8123}/",
            user=unquote(parts.username or "default"),
            password=unquote(parts.password or ""),
            database=(parts.path or "/dw").strip("/") or "dw",
        )


async def _ch_rows(ch: ClickHouseTarget, sql: str, *, attempts: int = 4) -> list[list[str]]:
    """Truy vấn chỉ ĐỌC, nên thử lại khi lỗi đường truyền là an toàn. Không thử lại thì một lần
    đứt kết nối (gặp trên compose đúng lúc DAG đang dựng lại bảng, 2026-09-24) làm CẢ lần đối
    soát chết — trong test ngâm 72h đó là mất phép đo, không phải phát hiện lỗi."""
    # Đăng nhập bằng header, không bằng query string (URL vào log của httpx — xem
    # `pipeline.clickhouse.ClickHouse._auth`).
    params = {"database": ch.database}
    headers = {"X-ClickHouse-User": ch.user, "X-ClickHouse-Key": ch.password}
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    ch.url, params=params, headers=headers, content=f"{sql} FORMAT TSV".encode()
                )
            break
        except httpx.TransportError:
            if attempt == attempts:
                raise
            await asyncio.sleep(0.5 * 2**attempt)
    if r.status_code != 200:
        raise RuntimeError(f"ClickHouse: {r.text.strip()[:500]}")
    return [line.split("\t") for line in r.text.splitlines() if line]


async def _pg_connect(dsn: str, *, attempts: int = 4) -> asyncpg.Connection:
    """Mở kết nối Postgres, thử lại khi lỗi ĐƯỜNG TRUYỀN — cùng lý do với `_ch_rows`.

    Gặp trên stack vừa dựng (2026-09-25): port-forward của Docker Desktop reset kết nối đầu tiên
    (`WinError 64`) và cả lần đối soát AT-01 chết. Lỗi xác thực, sai tên DB... không phải lỗi
    đường truyền: ném ngay, thử lại chỉ che cấu hình sai."""
    for attempt in range(1, attempts + 1):
        try:
            return await asyncpg.connect(dsn, timeout=15)
        except (OSError, asyncpg.exceptions.ConnectionDoesNotExistError, TimeoutError):
            if attempt == attempts:
                raise
            await asyncio.sleep(0.5 * 2**attempt)
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass(slots=True)
class _Sale:
    total: int
    business_date: str
    payments: dict[str, int]
    points: int
    #: Chỉ để đo độ tươi — KHÔNG thuộc phép so (mỗi tầng giữ nguyên giá trị cửa hàng đóng dấu,
    #: nhưng ClickHouse và Postgres trả khác độ chính xác, so vào thì chỉ sinh lệch giả).
    occurred_at: datetime | None = None


_EDGE_SALES = """
SELECT s.sale_id::text, s.total, s.business_date::text AS business_date, s.occurred_at,
       COALESCE((SELECT sum(l.delta) FROM point_ledger_local l WHERE l.sale_id = s.sale_id), 0)
           AS points
FROM sale s WHERE s.shift_id = ANY($1::uuid[])
"""
_EDGE_PAYMENTS = """
SELECT p.sale_id::text, p.method, sum(p.amount) AS amount
FROM sale_payment p JOIN sale s ON s.sale_id = p.sale_id
WHERE s.shift_id = ANY($1::uuid[]) GROUP BY p.sale_id, p.method
"""
_EDGE_SHIFTS = """
SELECT shift_id::text, status, expected_cash, variance FROM shift WHERE shift_id = ANY($1::uuid[])
"""
_EDGE_OUTBOX = """
SELECT count(*) FILTER (WHERE sent_at IS NULL AND dead_lettered_at IS NULL) AS pending,
       count(*) FILTER (WHERE dead_lettered_at IS NOT NULL) AS dead
FROM outbox
"""
_EDGE_CUSTOMER_POINTS = """
SELECT customer_id::text, sum(delta) AS points FROM point_ledger_local
WHERE customer_id = ANY($1::uuid[]) GROUP BY customer_id
"""

_CENTRAL_SALES = """
SELECT s.sale_id::text, s.total, s.business_date::text AS business_date, s.occurred_at,
       COALESCE((SELECT sum(l.delta) FROM point_ledger l
                  WHERE l.sale_id = s.sale_id AND l.store_id = s.store_id), 0) AS points
FROM sale_replica s WHERE s.store_id = $2 AND s.shift_id = ANY($1::uuid[])
"""
_CENTRAL_PAYMENTS = """
SELECT p.sale_id::text, p.method, sum(p.amount) AS amount
FROM sale_payment_replica p JOIN sale_replica s ON s.sale_id = p.sale_id
WHERE s.store_id = $2 AND s.shift_id = ANY($1::uuid[]) GROUP BY p.sale_id, p.method
"""
_CENTRAL_SHIFTS = """
SELECT shift_id::text, expected_cash, variance FROM shift_replica
WHERE store_id = $2 AND shift_id = ANY($1::uuid[])
"""
_CENTRAL_RECORDED = """
SELECT sale_id::text, recorded_at FROM sale_replica
WHERE store_id = $2 AND shift_id = ANY($1::uuid[])
"""
_CENTRAL_BALANCES = """
SELECT customer_id::text, balance FROM point_balance WHERE customer_id = ANY($1::uuid[])
"""


async def _sales(
    conn: asyncpg.Connection, sales_sql: str, pay_sql: str, *args: Any
) -> dict[str, _Sale]:
    out = {
        r["sale_id"]: _Sale(
            int(r["total"]), r["business_date"], {}, int(r["points"]), r["occurred_at"]
        )
        for r in await conn.fetch(sales_sql, *args)
    }
    for r in await conn.fetch(pay_sql, *args):
        if r["sale_id"] in out:
            out[r["sale_id"]].payments[r["method"]] = int(r["amount"])
    return out


def _summarise(sales: dict[str, _Sale]) -> dict[str, dict[str, int]]:
    days: dict[str, dict[str, int]] = defaultdict(
        lambda: {"sales": 0, "total": 0, "CASH": 0, "CARD": 0, "EWALLET": 0, "points": 0}
    )
    for s in sales.values():
        d = days[s.business_date]
        d["sales"] += 1
        d["total"] += s.total
        d["points"] += s.points
        for method, amount in s.payments.items():
            d[method] += amount
    return dict(days)


def _compare(
    upstream: dict[str, _Sale],
    downstream: dict[str, _Sale],
    *,
    store_id: str,
    layer: str,
    missing_level: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for sale_id, want in upstream.items():
        got = downstream.get(sale_id)
        if got is None:
            findings.append(Finding(missing_level, store_id, layer, "thiếu đơn", sale_id))
        elif (got.total, got.business_date, got.payments, got.points) != (
            want.total,
            want.business_date,
            want.payments,
            want.points,
        ):
            findings.append(Finding(DIVERGED, store_id, layer, f"sai số: {want} ≠ {got}", sale_id))
    for sale_id in downstream.keys() - upstream.keys():
        findings.append(Finding(DIVERGED, store_id, layer, "thừa đơn", sale_id))
    return findings


def _uuids(ids: Iterable[object]) -> str:
    """Danh sách hằng UUID cho SQL — mỗi giá trị đi qua `uuid.UUID()` nên không chèn được gì."""
    return ", ".join(f"toUUID('{uuid.UUID(str(i))}')" for i in ids)


async def _bronze(
    ch: ClickHouseTarget, store_id: str, shift_ids: list[uuid.UUID]
) -> tuple[dict[str, _Sale], list[Finding], Any]:
    """L3: phiên bản MỚI NHẤT của từng đơn trong bronze (một đơn bị UPDATE có nhiều phiên bản,
    docs/17 §4 bẫy 3). Cùng một phiên bản xuất hiện hai lần = khử trùng hỏng (bẫy 4)."""
    findings: list[Finding] = []
    if not shift_ids:
        return {}, findings, None
    shifts = _uuids(shift_ids)
    store = store_id.replace("'", "")
    sales_rows = await _ch_rows(
        ch,
        "SELECT toString(sale_id), argMax(total, recorded_at),"
        " toString(argMax(business_date, recorded_at)), count(), uniqExact(recorded_at),"
        " toString(argMax(occurred_at, recorded_at))"
        f" FROM bronze_sale WHERE store_id = '{store}' AND shift_id IN ({shifts})"
        " GROUP BY sale_id",
    )
    out: dict[str, _Sale] = {}
    for sid, total, bdate, n, versions, occurred in sales_rows:
        out[sid] = _Sale(int(total), bdate, {}, 0, _ch_ts(occurred))
        if int(n) != int(versions):
            findings.append(Finding(DIVERGED, store_id, "L2→L3", "đơn bị nạp đôi ở bronze", sid))
    if out:
        ids = _uuids(out)
        for sid, method, amount in await _ch_rows(
            ch,
            "SELECT toString(sale_id), method, sum(amount) FROM bronze_sale_payment"
            " WHERE (sale_id, recorded_at) IN (SELECT sale_id, max(recorded_at)"
            f" FROM bronze_sale_payment WHERE sale_id IN ({ids}) GROUP BY sale_id)"
            " GROUP BY sale_id, method",
        ):
            out[sid].payments[method] = int(amount)
        for sid, points, n, events in await _ch_rows(
            ch,
            "SELECT toString(sale_id), sum(delta), count(), uniqExact(event_id)"
            f" FROM bronze_point_ledger WHERE sale_id IN ({ids}) GROUP BY sale_id",
        ):
            out[sid].points = int(points)
            if int(n) != int(events):
                findings.append(
                    Finding(DIVERGED, store_id, "L2→L3", "điểm bị nạp đôi ở bronze", sid)
                )
    [[until]] = await _ch_rows(
        ch,
        "SELECT min(m) FROM (SELECT table_name, max(window_end) AS m FROM bronze_load_log"
        " WHERE table_name IN ('sale', 'sale_line', 'sale_payment', 'point_ledger')"
        " GROUP BY table_name) HAVING count() = 4",
    ) or [["\\N"]]
    return out, findings, until


def _virtual_store_layer(
    record: Any, l0: dict[str, _Sale], expected_points: dict[str, int]
) -> tuple[dict[str, _Sale], dict[str, dict[str, Any]], dict[str, int], dict[str, int]]:
    """ "L1" của cửa hàng ảo: đáp án chính là trạng thái của nó (xem docstring module)."""
    shifts = {
        sid: {
            "status": "CLOSED" if sh.closed else "OPEN",
            "expected_cash": sh.expected_cash,
            "variance": sh.variance,
        }
        for sid, sh in record.shifts.items()
    }
    outbox = {"pending": record.outbox.get("pending", 0), "dead": record.outbox.get("dead", 0)}
    return dict(l0), shifts, outbox, dict(expected_points)


async def _marts(
    ch: ClickHouseTarget, store_id: str, shift_ids: list[uuid.UUID]
) -> tuple[dict[str, _Sale], list[Finding], Any]:
    """L4: đơn như người phân tích thấy — đi qua `dim_store`/`dim_shift` (dim thiếu ca thì đơn
    vô hình với mọi truy vấn join, và bộ đối soát cũng phải thấy thế). Tổng đơn = Σ `net_amount`
    (line_total − chiết khấu phân bổ), phải bằng `total` tới từng đồng."""
    findings: list[Finding] = []
    [[present]] = await _ch_rows(
        ch,
        "SELECT count() FROM system.tables WHERE database = currentDatabase()"
        " AND name IN ('fact_sale_line', 'fact_payment', 'fact_point_event', 'dim_store',"
        " 'dim_shift')",
    )
    if int(present) != 5 or not shift_ids:
        return {}, findings, None  # dbt chưa chạy lần nào: mọi đơn "chưa tới"
    shifts = _uuids(shift_ids)
    store = store_id.replace("'", "")
    out: dict[str, _Sale] = {}
    for sid, net, bdate, n, lines, occurred in await _ch_rows(
        ch,
        "SELECT toString(sale_id), sum(net_amount), toString(any(business_date)), count(),"
        " uniqExact(line_no), toString(any(occurred_at)) FROM fact_sale_line"
        f" WHERE store_key IN (SELECT store_key FROM dim_store WHERE store_id = '{store}')"
        f" AND shift_key IN (SELECT shift_key FROM dim_shift WHERE shift_id IN ({shifts}))"
        " GROUP BY sale_id",
    ):
        out[sid] = _Sale(int(net), bdate, {}, 0, _ch_ts(occurred))
        if int(n) != int(lines):
            findings.append(Finding(DIVERGED, store_id, "L3→L4", "dòng hàng bị nhân đôi", sid))
    if out:
        ids = _uuids(out)
        for sid, method, amount, n, seqs in await _ch_rows(
            ch,
            "SELECT toString(sale_id), method, sum(amount), count(), uniqExact(seq)"
            f" FROM fact_payment WHERE sale_id IN ({ids}) GROUP BY sale_id, method",
        ):
            out[sid].payments[method] = int(amount)
            if int(n) != int(seqs):
                findings.append(Finding(DIVERGED, store_id, "L3→L4", "thanh toán bị nhân đôi", sid))
        for sid, points, n, events in await _ch_rows(
            ch,
            "SELECT toString(sale_id), sum(delta), count(), uniqExact(event_id)"
            f" FROM fact_point_event WHERE sale_id IN ({ids}) GROUP BY sale_id",
        ):
            out[sid].points = int(points)
            if int(n) != int(events):
                findings.append(Finding(DIVERGED, store_id, "L3→L4", "điểm bị nhân đôi", sid))
    # Mép "đã dựng tới": mọi đơn có recorded_at ≤ mốc này đã phải có trong mart (dbt dựng lại
    # trọn mọi tháng có dòng mới dưới mép cắt của nó — bẫy 5).
    [[until]] = await _ch_rows(
        ch,
        "SELECT least((SELECT max(_recorded_at) FROM fact_sale_line),"
        " (SELECT max(_recorded_at) FROM fact_payment))",
    )
    return out, findings, until


def _ch_ts(value: str) -> datetime | None:
    if value in ("", "\\N"):
        return None
    return datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=UTC)


def freshness(layers: dict[str, dict[str, _Sale]], *, now: datetime) -> dict[str, dict[str, Any]]:
    """Độ tươi của các đơn TRONG LẦN CHẠY ở từng tầng (docs/17 §3, §6).

    - `freshness_seconds` = now − `occurred_at` của đơn mới nhất đã có ở tầng đó — đúng định
      nghĩa docs, có nghĩa với chế độ `edge` (đồng hồ thật).
    - `behind_seconds` = tầng này tụt sau tầng TƯƠI NHẤT bao lâu. Vẫn đúng ở chế độ `virtual`,
      nơi `occurred_at` nằm trong quá khứ (ngày giả lập) nên `freshness_seconds` chỉ là tuổi
      của ngày giả lập, không phải độ trễ của luồng. 0 ở mọi tầng = luồng đã bắt kịp.

    Tầng không có `occurred_at` (L1 của cửa hàng ảo là chính manifest) hoặc chưa có đơn nào thì
    vắng mặt.
    """
    newest = {
        layer: max(times)
        for layer, sales in layers.items()
        if (times := [s.occurred_at for s in sales.values() if s.occurred_at is not None])
    }
    if not newest:
        return {}
    head = max(newest.values())
    return {
        layer: {
            "newest": ts.isoformat(),
            "freshness_seconds": round((now - ts).total_seconds(), 1),
            "behind_seconds": round((head - ts).total_seconds(), 1),
        }
        for layer, ts in newest.items()
    }


async def audit(
    manifest: Manifest,
    *,
    edge_dsns: dict[str, str],
    central_dsn: str,
    clickhouse: ClickHouseTarget | None = None,
    marts: bool = False,
) -> AuditReport:
    """`clickhouse` bật L3 (bronze); thêm `marts=True` bật L4 (cần dbt đã chạy)."""
    report = AuditReport(status=CONVERGED)
    central = await _pg_connect(central_dsn)
    try:
        for store_id, record in manifest.stores.items():
            shift_ids = [uuid.UUID(s) for s in record.shifts]
            l0 = {
                sid: _Sale(s.total, s.business_date, dict(s.payments), s.points)
                for sid, s in record.sales.items()
            }
            expected_points = manifest.expected_points(store_id)
            customer_ids = [uuid.UUID(c) for c in expected_points]
            if store_id not in edge_dsns and manifest.mode == "virtual":
                l1, l1_shifts, outbox, l1_points = _virtual_store_layer(record, l0, expected_points)
            else:
                edge = await _pg_connect(edge_dsns[store_id])
                try:
                    l1 = await _sales(edge, _EDGE_SALES, _EDGE_PAYMENTS, shift_ids)
                    l1_shifts = {
                        r["shift_id"]: dict(r) for r in await edge.fetch(_EDGE_SHIFTS, shift_ids)
                    }
                    outbox = dict(await edge.fetchrow(_EDGE_OUTBOX))
                    l1_points = {
                        r["customer_id"]: int(r["points"])
                        for r in await edge.fetch(_EDGE_CUSTOMER_POINTS, customer_ids)
                    }
                finally:
                    await edge.close()
            l2 = await _sales(central, _CENTRAL_SALES, _CENTRAL_PAYMENTS, shift_ids, store_id)
            l2_shifts = {
                r["shift_id"]: r for r in await central.fetch(_CENTRAL_SHIFTS, shift_ids, store_id)
            }
            l2_points = {
                r["customer_id"]: int(r["balance"])
                for r in await central.fetch(_CENTRAL_BALANCES, customer_ids)
            }

            report.outbox[store_id] = outbox
            report.summary[store_id] = {
                "L0": _summarise(l0),
                "L1": _summarise(l1),
                "L2": _summarise(l2),
            }
            if clickhouse is not None:
                recorded = {
                    r["sale_id"]: r["recorded_at"]
                    for r in await central.fetch(_CENTRAL_RECORDED, shift_ids, store_id)
                }
                l3, l3_findings, until = await _bronze(clickhouse, store_id, shift_ids)
                report.summary[store_id]["L3"] = _summarise(l3)
                report.findings.extend(l3_findings)
                report.findings.extend(
                    _compare_downstream(
                        l2, l3, recorded, until, store_id=store_id, layer="L2→L3", inclusive=False
                    )
                )
                if marts:
                    # So với L3, không với L2: lệch ở đây là lỗi của S6, không lẫn lỗi S4/S5.
                    l4, l4_findings, built = await _marts(clickhouse, store_id, shift_ids)
                    report.summary[store_id]["L4"] = _summarise(l4)
                    report.findings.extend(l4_findings)
                    report.findings.extend(
                        _compare_downstream(
                            l3,
                            l4,
                            recorded,
                            built,
                            store_id=store_id,
                            layer="L3→L4",
                            inclusive=True,
                        )
                    )
            layers = {"L1": l1, "L2": l2}
            if clickhouse is not None:
                layers["L3"] = l3
                if marts:
                    layers["L4"] = l4
            report.freshness[store_id] = freshness(layers, now=datetime.now(UTC))

            pending = outbox["pending"] > 0
            if outbox["dead"]:
                report.findings.append(
                    Finding(DIVERGED, store_id, "L1→L2", f"{outbox['dead']} sự kiện dead-letter")
                )

            # ── L0 → L1: cửa hàng phải giữ ĐÚNG mọi đơn đã trả 201 ──
            unknown_sales = sum(1 for u in record.unknown if u["kind"] == "sale")
            l0_findings = _compare(
                l0,
                {k: v for k, v in l1.items() if k in l0},
                store_id=store_id,
                layer="L0→L1",
                missing_level=DIVERGED,
            )
            report.findings.extend(l0_findings)
            extra = len(l1.keys() - l0.keys())
            if extra > unknown_sales:
                report.findings.append(
                    Finding(
                        DIVERGED,
                        store_id,
                        "L0→L1",
                        f"{extra} đơn ở cửa hàng không có trong đáp án"
                        f" (chỉ {unknown_sales} không rõ kết cục)",
                    )
                )
            for shift_id, shift in record.shifts.items():
                row = l1_shifts.get(shift_id)
                if not shift.closed:
                    continue
                if (
                    row is None
                    or row["status"] != "CLOSED"
                    or (row["expected_cash"], row["variance"])
                    != (shift.expected_cash, shift.variance)
                ):
                    report.findings.append(
                        Finding(DIVERGED, store_id, "L0→L1", "ca không khớp đáp án", shift_id)
                    )
                elif shift.variance != 0 and unknown_sales == 0:
                    # Bộ giả lập đếm két bằng đúng số tiền mặt nó đã trả qua các đơn 201.
                    report.findings.append(
                        Finding(
                            DIVERGED, store_id, "L0→L1", f"chênh két {shift.variance}", shift_id
                        )
                    )

            # ── L1 → L2: trung tâm phải có đúng những gì cửa hàng có ──
            report.findings.extend(
                _compare(
                    l1,
                    l2,
                    store_id=store_id,
                    layer="L1→L2",
                    missing_level=CONVERGING if pending else DIVERGED,
                )
            )
            for shift_id, row in l1_shifts.items():
                if row["status"] != "CLOSED":
                    continue
                c = l2_shifts.get(shift_id)
                if c is None:
                    level = CONVERGING if pending else DIVERGED
                    report.findings.append(Finding(level, store_id, "L1→L2", "thiếu ca", shift_id))
                elif (c["expected_cash"], c["variance"]) != (row["expected_cash"], row["variance"]):
                    report.findings.append(Finding(DIVERGED, store_id, "L1→L2", "sai ca", shift_id))

            # ── Điểm của khách mới trong lần chạy (docs/17 §5, INV-4 theo đáp án) ──
            strict = not any(u["kind"] in ("sale", "customer") for u in record.unknown)
            for customer_id, want in expected_points.items():
                if strict and l1_points.get(customer_id, 0) != want:
                    report.findings.append(
                        Finding(
                            DIVERGED,
                            store_id,
                            "L0→L1",
                            f"điểm khách {l1_points.get(customer_id, 0)} ≠ {want}",
                            customer_id,
                        )
                    )
                got = l2_points.get(customer_id, 0)
                target = l1_points.get(customer_id, 0)
                if got != target:
                    level = CONVERGING if pending and got < target else DIVERGED
                    report.findings.append(
                        Finding(
                            level, store_id, "L1→L2", f"điểm khách {got} ≠ {target}", customer_id
                        )
                    )
    finally:
        await central.close()

    levels = {f.level for f in report.findings}
    report.status = DIVERGED if DIVERGED in levels else CONVERGING if levels else CONVERGED
    return report


def _compare_downstream(
    upstream: dict[str, _Sale],
    downstream: dict[str, _Sale],
    recorded: dict[str, Any],
    until: Any,
    *,
    store_id: str,
    layer: str,
    inclusive: bool,
) -> list[Finding]:
    """Tầng trong ClickHouse chạy theo lô, nên "thiếu" có hai nghĩa. Thiếu mà `recorded_at` (ở
    trung tâm) đã nằm dưới mép tầng đó đã xử lý tới → MẤT (DIVERGED); trên mép → lô chưa tới
    lượt (CONVERGING). Thừa hay sai số → DIVERGED.

    L3: mép = "đã nạp tới" (nhật ký nạp, mép cửa sổ loại trừ). L4: mép = `_recorded_at` lớn nhất
    trong fact (dòng đó đã vào, nên tính cả chính mốc — `inclusive`)."""
    from datetime import UTC, datetime

    edge = (
        None
        if until in (None, "\\N")
        else datetime.fromisoformat(str(until).replace(" ", "T")).replace(tzinfo=UTC)
    )
    findings: list[Finding] = []
    for sale_id, want in upstream.items():
        got = downstream.get(sale_id)
        if got is None:
            at = recorded[sale_id]
            lost = edge is not None and (at <= edge if inclusive else at < edge)
            findings.append(
                Finding(DIVERGED if lost else CONVERGING, store_id, layer, "thiếu đơn", sale_id)
            )
        elif (got.total, got.business_date, got.payments, got.points) != (
            want.total,
            want.business_date,
            want.payments,
            want.points,
        ):
            findings.append(Finding(DIVERGED, store_id, layer, f"sai số: {want} ≠ {got}", sale_id))
    for sale_id in downstream.keys() - upstream.keys():
        findings.append(Finding(DIVERGED, store_id, layer, "thừa đơn", sale_id))
    return findings


async def audit_until_settled(
    manifest: Manifest,
    *,
    edge_dsns: dict[str, str],
    central_dsn: str,
    wait_seconds: float,
    poll_seconds: float = 2.0,
    clickhouse: ClickHouseTarget | None = None,
    marts: bool = False,
) -> AuditReport:
    """Đối soát lại tới khi hết `CONVERGING`. Quá hạn mà vẫn thiếu → `DIVERGED`.

    "Chưa tới" chỉ chấp nhận được trong thời hạn hội tụ; quá hạn thì chính việc chưa tới
    là sự cố (NFR-04: điểm hội tụ < 5 phút khi mạng bình thường).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while True:
        report = await audit(
            manifest,
            edge_dsns=edge_dsns,
            central_dsn=central_dsn,
            clickhouse=clickhouse,
            marts=marts,
        )
        if report.status != CONVERGING or loop.time() >= deadline:
            break
        await asyncio.sleep(poll_seconds)
    if report.status == CONVERGING:
        report.status = DIVERGED
        report.findings.append(
            Finding(DIVERGED, "*", "*", f"quá hạn hội tụ {wait_seconds:.0f}s mà vẫn thiếu")
        )
    return report
