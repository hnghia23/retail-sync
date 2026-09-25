"""Chế độ `edge` — phát lịch ý định vào Edge API THẬT (docs/18 §3).

Đi đúng đường một quầy thu ngân đi: đăng nhập → mở ca → (đăng ký khách) → tạm tính → chốt
đơn → đóng ca. Không `INSERT` thẳng vào DB cửa hàng (ADR-010 quy tắc 3).

## Vòng hở

Mỗi đơn là một task riêng, khởi chạy ĐÚNG giờ đã lên lịch, không đợi đơn trước trả lời.
Hệ thống chậm thì đơn chồng lên nhau — đúng như quầy thật lúc cao điểm — và độ trễ đo được
là độ trễ thật (không có coordinated omission, docs/18 §1). Ngoại lệ duy nhất: đóng ca ĐỢI
các đơn của ca đó xong, vì quầy thật không đếm két khi đang còn khách đứng thanh toán.

`scheduler_lag_s` ghi mỗi lần bộ giả lập tới trễ lịch của chính nó. Trễ đáng kể nghĩa là
bộ giả lập đang là nút thắt, và phép đo của lần chạy đó không hợp lệ.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from simulator.generator import CloseShift, OpenShift, Sale
from simulator.manifest import SaleRecord, ShiftRecord

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from simulator.generator import Intent
    from simulator.manifest import Manifest, StoreRecord

__all__ = ["StoreTarget", "payment_split", "run_edge"]

API = "/api/v1"


@dataclass(frozen=True, slots=True)
class StoreTarget:
    store_id: str
    client: httpx.AsyncClient
    #: Độ lệch giờ cửa hàng — để khai `business_date` = hôm nay theo giờ cửa hàng.
    utc_offset_minutes: int = 420


@dataclass(slots=True)
class _Shift:
    record: ShiftRecord
    tasks: list[asyncio.Task[None]] = field(default_factory=list)


def payment_split(plan: str, total: int) -> list[dict[str, Any]]:
    """Kế hoạch thanh toán → các dòng thanh toán. Dùng chung cho chế độ `edge` và `virtual`."""
    if plan == "SPLIT":
        cash = total // 2
        return [{"method": "CASH", "amount": cash}, {"method": "CARD", "amount": total - cash}]
    return [{"method": plan, "amount": total}]


def _error_code(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        return str(response.status_code)
    if isinstance(detail, dict):
        return str(detail.get("code", response.status_code))
    return str(response.status_code)


class _StoreRun:
    def __init__(
        self, target: StoreTarget, *, password: str, manifest: Manifest, opening_cash: int
    ) -> None:
        self.t = target
        self.password = password
        self.m = manifest
        self.rec: StoreRecord = manifest.store(target.store_id)
        self.opening_cash = opening_cash
        self.employees = {
            "cashier": f"{target.store_id}-cash-01",
            "manager": f"{target.store_id}-mgr-01",
        }
        self.tokens: dict[str, str] = {}
        #: Mở/đóng ca thử lại tối đa bao lâu khi cửa hàng tạm chết (`call_retrying`).
        self.retry_attempts = 60
        self.retry_delay = 1.0
        self.customers: dict[str, asyncio.Future[str | None]] = {}
        self.shift: _Shift | None = None

    # ─────────────── HTTP ───────────────

    async def _login(self, role: str) -> None:
        r = await self.t.client.post(
            f"{API}/auth/login",
            json={"employee_id": self.employees[role], "password": self.password},
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"{self.t.store_id}: đăng nhập {self.employees[role]} thất bại ({r.status_code})."
                " Đã seed nhân viên chưa? (make seed-employees)"
            )
        self.tokens[role] = r.json()["access_token"]

    async def call(
        self, path: str, body: dict[str, Any], *, role: str, endpoint: str
    ) -> httpx.Response:
        """POST có đo thời gian; hết hạn token (15 phút) → đăng nhập lại MỘT lần.

        Thử lại sau `401` là an toàn: `401` nghĩa là request bị từ chối trước khi làm gì.
        Không thử lại với bất kỳ lỗi nào khác — thử lại `POST /sales` sau timeout có thể
        tạo đơn thứ hai.
        """
        for attempt in (1, 2):
            started = time.perf_counter()
            r = await self.t.client.post(
                f"{API}{path}", json=body, headers={"Authorization": f"Bearer {self.tokens[role]}"}
            )
            self.m.observe(endpoint, (time.perf_counter() - started) * 1000)
            if r.status_code == 401 and attempt == 1:
                await self._login(role)
                continue
            return r
        raise AssertionError("unreachable")

    async def call_retrying(
        self, path: str, body: dict[str, Any], *, role: str, endpoint: str
    ) -> httpx.Response | None:
        """Cho mở/đóng ca: cửa hàng tạm chết (test hỗn loạn — `kill -9` app hay Postgres) thì thu
        ngân bấm lại cho tới khi được. `None` = hết lượt mà vẫn không tới được.

        An toàn để lặp: mở ca lần hai trả `409 SHIFT_ALREADY_OPEN` kèm `shift_id` (xử lý ở
        `open_shift`), đóng ca lần hai trả `409` — không bao giờ tạo ra ca thứ hai."""
        for _ in range(self.retry_attempts):
            try:
                r = await self.call(path, body, role=role, endpoint=endpoint)
            except httpx.HTTPError:
                r = None
            if r is not None and r.status_code < 500:
                return r
            await asyncio.sleep(self.retry_delay)
        return None

    # ─────────────── Ca ───────────────

    def _today(self) -> str:
        return (datetime.now(UTC) + timedelta(minutes=self.t.utc_offset_minutes)).date().isoformat()

    async def open_shift(self) -> None:
        body = {"business_date": self._today(), "opening_cash": self.opening_cash}
        r = await self.call_retrying("/shifts/open", body, role="cashier", endpoint="shifts/open")
        if r is None:
            self.rec.rejected.append({"kind": "open_shift", "code": "UNREACHABLE"})
            self.shift = None
            return
        if r.status_code == 409 and _error_code(r) == "SHIFT_ALREADY_OPEN":
            # Ca còn mở từ trước lần chạy (hoặc lần chạy trước chết giữa chừng). Đóng nó để
            # bắt đầu sạch, và ghi lại — ca đó KHÔNG thuộc đáp án của lần chạy này.
            foreign = r.json()["detail"]["shift_id"]
            closed = await self.call(
                f"/shifts/{foreign}/close",
                {"counted_cash": 0, "variance_note": f"simulator {self.m.run_id}: đóng ca cũ"},
                role="manager",
                endpoint="shifts/close",
            )
            self.rec.foreign_shifts.append(foreign)
            if closed.status_code != 200:
                self.rec.rejected.append(
                    {
                        "kind": "close_foreign_shift",
                        "shift_id": foreign,
                        "code": _error_code(closed),
                    }
                )
            r = await self.call_retrying(
                "/shifts/open", body, role="cashier", endpoint="shifts/open"
            )
        if r is None or r.status_code != 201:
            code = "UNREACHABLE" if r is None else _error_code(r)
            self.rec.rejected.append({"kind": "open_shift", "code": code})
            self.shift = None
            return
        data = r.json()
        record = ShiftRecord(
            shift_id=data["shift_id"],
            business_date=data["business_date"],
            opening_cash=self.opening_cash,
        )
        self.rec.shifts[record.shift_id] = record
        self.shift = _Shift(record)

    async def close_shift(self) -> None:
        shift = self.shift
        if shift is None:
            return
        await asyncio.gather(*shift.tasks)
        counted = shift.record.opening_cash + shift.record.cash_from_sales
        r = await self.call_retrying(
            f"/shifts/{shift.record.shift_id}/close",
            {"counted_cash": counted},
            role="manager",
            endpoint="shifts/close",
        )
        if r is not None and r.status_code == 200:
            data = r.json()
            shift.record.closed = True
            shift.record.counted_cash = counted
            shift.record.expected_cash = data["expected_cash"]
            shift.record.variance = data["variance"]
        else:
            # Kể cả `409` sau một lần mất kết nối (lần trước đã đóng mà mất phản hồi): không biết
            # số két trung tâm tính ra, nên ca này không vào đáp án — bộ đối soát bỏ qua nó.
            code = "UNREACHABLE" if r is None else _error_code(r)
            self.rec.rejected.append(
                {"kind": "close_shift", "shift_id": shift.record.shift_id, "code": code}
            )
        self.shift = None

    # ─────────────── Đơn ───────────────

    def schedule_sale(self, sale: Sale) -> None:
        if sale.customer is not None and sale.customer.new:
            # Tạo future NGAY (đồng bộ, trước khi task chạy): đơn sau của cùng khách có thể
            # được lên lịch trước khi đơn đăng ký kịp chạy tới dòng đầu tiên.
            self.customers[sale.customer.ref] = asyncio.get_running_loop().create_future()
        shift = self.shift
        if shift is None:
            self.rec.rejected.append({"kind": "sale", "intent": sale.intent_id, "code": "NO_SHIFT"})
            if sale.customer is not None and sale.customer.new:
                self.customers[sale.customer.ref].set_result(None)  # khách chưa bao giờ tồn tại
            return
        shift.tasks.append(asyncio.create_task(self._sale(sale, shift)))

    async def _customer_for(self, sale: Sale) -> str | None:
        ref = sale.customer
        if ref is None:
            return None
        future = self.customers.get(ref.ref)
        if future is None:
            return None
        if not ref.new:
            return await future
        customer_id: str | None = None
        try:
            r = await self.call(
                "/customers", {"phone": ref.phone}, role="cashier", endpoint="customers"
            )
            if r.status_code in (200, 201):
                data = r.json()
                customer_id = data["customer_id"]
                self.rec.customers[data["customer_id"]] = {"created": bool(data["created"])}
            elif r.status_code >= 500:
                self.rec.unknown.append(
                    {"kind": "customer", "intent": sale.intent_id, "error": str(r.status_code)}
                )
            else:
                self.rec.rejected.append(
                    {"kind": "customer", "intent": sale.intent_id, "code": _error_code(r)}
                )
        except httpx.HTTPError as exc:
            # Đăng ký không rõ kết cục: bán tiếp như khách vãng lai, đúng hành vi quầy thật.
            self.rec.unknown.append(
                {"kind": "customer", "intent": sale.intent_id, "error": type(exc).__name__}
            )
        finally:
            future.set_result(customer_id)
        return customer_id

    async def _sale(self, sale: Sale, shift: _Shift) -> None:
        customer_id = await self._customer_for(sale)
        lines = [{"product_id": p, "quantity": q} for p, q in sale.lines]
        try:
            q = await self.call(
                "/sales/quote",
                {"lines": lines, "customer_id": customer_id},
                role="cashier",
                endpoint="sales/quote",
            )
        except httpx.HTTPError as exc:
            # Tạm tính không ghi gì — mất kết nối ở đây là "không bán", không phải "không rõ".
            self.rec.rejected.append(
                {"kind": "quote", "intent": sale.intent_id, "code": type(exc).__name__}
            )
            return
        if q.status_code != 200:
            self.rec.rejected.append(
                {"kind": "quote", "intent": sale.intent_id, "code": _error_code(q)}
            )
            return

        total = int(q.json()["total"])
        payments = payment_split(sale.payment, total)
        body = {
            "employee_id": self.employees["cashier"],
            "shift_id": shift.record.shift_id,
            "customer_id": customer_id,
            "lines": lines,
            "payments": payments,
        }
        try:
            r = await self.call("/sales", body, role="cashier", endpoint="sales")
        except httpx.HTTPError as exc:
            self.rec.unknown.append(
                {
                    "kind": "sale",
                    "intent": sale.intent_id,
                    "shift_id": shift.record.shift_id,
                    "error": type(exc).__name__,
                }
            )
            return
        if r.status_code >= 500:
            # Máy chủ chết GIỮA lúc chốt (CH-2 `kill -9` app, CH-3 `kill -9` Postgres): commit có
            # thể đã xong mà phản hồi mất. `4xx` thì chắc chắn chưa ghi gì; `5xx` thì không biết.
            # Xếp nhầm vào `rejected` là bộ đối soát thấy "đơn thừa" ở cửa hàng và báo DIVERGED.
            self.rec.unknown.append(
                {
                    "kind": "sale",
                    "intent": sale.intent_id,
                    "shift_id": shift.record.shift_id,
                    "error": str(r.status_code),
                }
            )
            return
        if r.status_code != 201:
            self.rec.rejected.append(
                {"kind": "sale", "intent": sale.intent_id, "code": _error_code(r)}
            )
            return

        data = r.json()
        paid = {p["method"]: p["amount"] for p in payments}
        self.rec.sales[data["sale_id"]] = SaleRecord(
            sale_id=data["sale_id"],
            shift_id=shift.record.shift_id,
            business_date=shift.record.business_date,
            total=int(data["total"]),
            payments=paid,
            points=int(data["points_earned"] or 0),
            customer_id=customer_id,
        )
        shift.record.cash_from_sales += paid.get("CASH", 0)


async def _run_store(
    target: StoreTarget,
    intents: Sequence[Intent],
    *,
    password: str,
    manifest: Manifest,
    opening_cash: int,
    start: float,
) -> None:
    loop = asyncio.get_running_loop()
    run = _StoreRun(target, password=password, manifest=manifest, opening_cash=opening_cash)
    await run._login("cashier")
    await run._login("manager")

    for intent in intents:
        delay = start + intent.at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            manifest.scheduler_lag_s.append(-delay)
        if isinstance(intent, OpenShift):
            await run.open_shift()
        elif isinstance(intent, CloseShift):
            await run.close_shift()
        else:
            run.schedule_sale(intent)
    await run.close_shift()  # phòng lịch kết thúc giữa ca


async def run_edge(
    targets: Sequence[StoreTarget],
    plans: Mapping[str, Sequence[Intent]],
    *,
    password: str,
    manifest: Manifest,
    opening_cash: int,
    lead_seconds: float = 0.5,
) -> Manifest:
    """Chạy mọi cửa hàng song song trên cùng một đồng hồ; ghi đáp án vào `manifest`."""
    loop = asyncio.get_running_loop()
    start = loop.time() + lead_seconds
    cpu0, wall0 = time.process_time(), time.perf_counter()
    await asyncio.gather(
        *(
            _run_store(
                t,
                plans[t.store_id],
                password=password,
                manifest=manifest,
                opening_cash=opening_cash,
                start=start,
            )
            for t in targets
        )
    )
    wall = time.perf_counter() - wall0
    manifest.cpu_share = round((time.process_time() - cpu0) / wall, 4) if wall > 0 else None
    manifest.finished_at = datetime.now(UTC).isoformat()
    return manifest
