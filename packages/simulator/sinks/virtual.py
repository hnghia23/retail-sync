"""Chế độ `virtual` — cửa hàng ảo đẩy thẳng lên `POST /events` của trung tâm (docs/18 §3).

Một tiến trình đóng vai N cửa hàng. Mỗi cửa hàng ảo có:

- **Tính tiền và tính điểm bằng domain THẬT của edge** (`price_cart`, `resolve_tier`,
  `points_earned_for`) và payload bằng model THẬT của `shared.events`. Payload sai hợp đồng
  (INV-1, INV-2) không dựng được ngay từ đầu. Không có bộ tính tiền thứ hai để lệch.
- **Một outbox trong bộ nhớ**, xả bằng `HttpCentralClient` + `next_backoff` THẬT của
  `edge.sync`. Lô ≤ `SYNC_BATCH_SIZE`, tôn trọng `Retry-After`, lùi có jitter. Quy tắc bất khả
  xâm phạm của worker (CLAUDE.md): lỗi ĐƯỜNG TRUYỀN không bao giờ tăng `attempts`, chỉ lần trung
  tâm xét và từ chối mới tính một lượt.
- **Mạng có công tắc** (tật `offline`): transport ném `ConnectError` như rút dây mạng, và chính
  `HttpCentralClient` dịch nó thành `CentralUnavailableError` như với worker thật.

Thời gian là tổng hợp: `occurred_at` = giờ mở cửa của ngày giả lập + vị trí trong ngày. Trung tâm
vốn tin đồng hồ cửa hàng. Ngày giả lập nằm trong QUÁ KHỨ (mặc định kết thúc hôm qua), để
`recorded_at` luôn sau `occurred_at` — như một cửa hàng đồng bộ trễ.

Đáp án (manifest) là chính các envelope đã dựng: cửa hàng ảo "commit" mọi thứ nó tạo ra.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from edge.loyalty.domain.points import EarnRule, points_earned_for
from edge.loyalty.domain.tier import resolve_tier
from edge.pos.domain.pricing import CartLine, price_cart
from edge.sync.backoff import next_backoff
from edge.sync.client import CentralUnavailableError, HttpCentralClient
from shared.config import PricingRules, SyncSettings
from shared.events import (
    CustomerPayload,
    EventType,
    PointsPayload,
    SaleCompletedPayload,
    SaleLinePayload,
    SalePaymentPayload,
    ShiftClosedPayload,
    build_envelope,
)
from shared.pii import hash_phone
from shared.seed_data import demo_employees
from shared.types import new_event_id
from simulator.generator import CloseShift, OpenShift, Sale
from simulator.manifest import SaleRecord, ShiftRecord
from simulator.sinks.edge_http import payment_split

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import BaseModel

    from shared.events import IngestResult
    from simulator.generator import Intent
    from simulator.manifest import Manifest
    from simulator.profile import Profile
    from simulator.quirks import Quirks

__all__ = ["VirtualTarget", "read_keys", "run_virtual"]


@dataclass(frozen=True, slots=True)
class VirtualTarget:
    store_id: str
    api_key: str


def read_keys(text: str) -> list[VirtualTarget]:
    """File khóa do `central.ops.provision_store --keys-out` ghi: mỗi dòng `store_id=khóa`."""
    out: list[VirtualTarget] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        store_id, sep, key = line.partition("=")
        if not sep or not key:
            raise ValueError(f"dòng khóa không hợp lệ: {store_id!r}")
        out.append(VirtualTarget(store_id.strip(), key.strip()))
    return out


class _Network(httpx.AsyncBaseTransport):
    """Transport có công tắc: offline = `ConnectError`, giống hệt lúc rút dây mạng."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.online = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.online:
            raise httpx.ConnectError("simulated offline (tật `offline`)", request=request)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass(slots=True)
class _Pending:
    envelope: dict[str, object]
    attempts: int = 0
    not_before: float = 0.0


@dataclass(slots=True)
class _Central:
    """Thứ trung tâm ĐÃ biết — cửa hàng khác chỉ "tra" được khách đã tới trung tâm."""

    customers: dict[str, uuid.UUID] = field(default_factory=dict)


@dataclass(slots=True)
class _Clock:
    """Giây giả lập (kể từ giờ mở cửa ngày đầu) → mốc UTC và ngày kinh doanh."""

    profile: Profile
    start_date: date
    rate: float
    utc_offset: timedelta

    def at(self, real_at: float) -> tuple[datetime, date]:
        sim = real_at * self.rate
        day_seconds = self.profile.day_hours * 3600
        day = int(sim // day_seconds)
        business_date = self.start_date + timedelta(days=day)
        # Giờ cửa hàng tính như UTC rồi trừ độ lệch — không cần bảng múi giờ (VN không có DST).
        local = datetime(
            business_date.year,
            business_date.month,
            business_date.day,
            self.profile.open_hour,
            tzinfo=UTC,
        ) + timedelta(seconds=sim - day * day_seconds)
        return local - self.utc_offset, business_date


class _VirtualStore:
    def __init__(
        self,
        target: VirtualTarget,
        *,
        client: HttpCentralClient,
        network: _Network,
        manifest: Manifest,
        settings: SyncSettings,
        clock: _Clock,
        prices: Mapping[str, int],
        opening_cash: int,
        pii_key: str,
        central: _Central,
        quirks: Quirks,
        offline: tuple[float, float] | None,
        seed: int,
    ) -> None:
        self.store_id = target.store_id
        self.client = client
        self.network = network
        self.manifest = manifest
        self.record = manifest.store(self.store_id)
        self.settings = settings
        self.clock = clock
        self.prices = prices
        self.opening_cash = opening_cash
        self.pii_key = pii_key
        self.central = central
        self.quirks = quirks
        #: Khoảng offline theo giây THẬT kể từ lúc bắt đầu chạy.
        self.offline = offline
        self.rng = random.Random(f"{seed}:{self.store_id}:virtual")  # noqa: S311
        self.pricing = PricingRules()
        self.earn = EarnRule()
        employees = demo_employees(self.store_id)
        self.manager = employees[0].employee_id
        self.cashiers = [e.employee_id for e in employees[1:]]

        # Outbox: dict giữ thứ tự chèn = `ORDER BY id` của outbox thật.
        self.outbox: dict[str, _Pending] = {}
        self.producing = True
        self.own_customers: dict[str, uuid.UUID] = {}
        #: event_id của `CustomerCreated` → ref, để báo "trung tâm đã biết khách" khi được nhận.
        self.customer_events: dict[str, tuple[str, uuid.UUID]] = {}
        #: Điểm tích lũy MÀ CỬA HÀNG NÀY biết — nguồn của hạng lúc bán (như `customer_local`).
        self.lifetime: dict[uuid.UUID, int] = {}
        self.shift: ShiftRecord | None = None
        self.shift_opened_at: datetime | None = None
        self.shift_cashier = ""
        self.stats = self.record.outbox
        for key in ("pending", "dead", "sent", "transport_errors", "resent_batches",
                    "resend_mismatch", "foreign_customer_sales", "foreign_fallback"):  # fmt: skip
            self.stats.setdefault(key, 0)

    # ───────────── sinh sự kiện (cửa hàng "commit") ─────────────

    def _enqueue(
        self,
        event_type: EventType,
        payload: BaseModel,
        occurred_at: datetime,
        *,
        event_id: uuid.UUID | None = None,
    ) -> str:
        eid = event_id or new_event_id()
        self.outbox[str(eid)] = _Pending(
            build_envelope(
                event_id=eid,
                event_type=event_type,
                store_id=self.store_id,
                occurred_at=occurred_at,
                payload=payload,
            )
        )
        return str(eid)

    def apply(self, intent: Intent) -> None:
        occurred_at, business_date = self.clock.at(intent.at)
        if isinstance(intent, OpenShift):
            self._open_shift(intent, occurred_at, business_date)
        elif isinstance(intent, CloseShift):
            self._close_shift(occurred_at)
        else:
            self._sale(intent, occurred_at, business_date)

    def _open_shift(self, intent: OpenShift, at: datetime, business_date: date) -> None:
        shift_id = str(new_event_id())
        self.shift = ShiftRecord(shift_id, business_date.isoformat(), self.opening_cash)
        self.record.shifts[shift_id] = self.shift
        self.shift_opened_at = at
        self.shift_cashier = self.cashiers[intent.shift_no % len(self.cashiers)]

    def _close_shift(self, at: datetime) -> None:
        shift, opened_at = self.shift, self.shift_opened_at
        if shift is None or opened_at is None:
            return
        expected = shift.opening_cash + shift.cash_from_sales
        self._enqueue(
            "ShiftClosed",
            ShiftClosedPayload(
                shift_id=uuid.UUID(shift.shift_id),
                business_date=date.fromisoformat(shift.business_date),
                opened_by_employee_id=self.shift_cashier,
                closed_by_employee_id=self.manager,
                opened_at=opened_at,
                closed_at=at,
                opening_cash=shift.opening_cash,
                expected_cash=expected,
                counted_cash=expected,
                variance=0,
            ),
            at,
        )
        shift.closed, shift.expected_cash, shift.counted_cash, shift.variance = (
            True,
            expected,
            expected,
            0,
        )
        self.shift = None

    def _customer(self, intent: Sale, at: datetime) -> uuid.UUID | None:
        ref = intent.customer
        if ref is None:
            return None
        if ref.new:
            customer_id = new_event_id()
            self.own_customers[ref.ref] = customer_id
            self.record.customers[str(customer_id)] = {"created": True}
            eid = self._enqueue(
                "CustomerCreated",
                CustomerPayload(
                    customer_id=customer_id,
                    phone_hash=hash_phone(ref.phone, key=self.pii_key),
                    created_locally_at_store=self.store_id,
                ),
                at,
            )
            self.customer_events[eid] = (ref.ref, customer_id)
            return customer_id
        if ref.ref in self.own_customers:
            return self.own_customers[ref.ref]
        # Khách của cửa hàng khác (tật `concurrent_customer`): chỉ tra được qua trung tâm.
        known = self.central.customers.get(ref.ref)
        self.stats["foreign_customer_sales" if known else "foreign_fallback"] += 1
        return known

    def _sale(self, intent: Sale, at: datetime, business_date: date) -> None:
        shift = self.shift
        if shift is None:
            return  # ngoài ca — generator không xếp đơn ở đây, phòng hờ
        customer_id = self._customer(intent, at)
        lifetime = self.lifetime.get(customer_id, 0) if customer_id else 0
        tier_pct = resolve_tier(lifetime).discount_pct if customer_id else 0
        priced = price_cart(
            [CartLine(pid, self.prices[pid], qty) for pid, qty in intent.lines],
            rules=self.pricing,
            tier_discount_pct=tier_pct,
        )
        payments = payment_split(intent.payment, priced.total)
        sale_id = new_event_id()
        self._enqueue(
            "SaleCompleted",
            SaleCompletedPayload(
                sale_id=sale_id,
                shift_id=uuid.UUID(shift.shift_id),
                employee_id=self.shift_cashier,
                customer_id=customer_id,
                business_date=business_date,
                subtotal=priced.subtotal,
                discount_tier=priced.discount_tier,
                discount_promo=priced.discount_promo,
                total=priced.total,
                lines=[
                    SaleLinePayload(
                        line_no=line.line_no,
                        product_id=line.product_id,
                        quantity=line.quantity,
                        unit_price=line.unit_price,
                        line_total=line.line_total,
                    )
                    for line in priced.lines
                ],
                payments=[
                    SalePaymentPayload(seq=i, method=p["method"], amount=p["amount"])
                    for i, p in enumerate(payments, start=1)
                ],
            ),
            at,
        )
        points = points_earned_for(priced.total, self.earn) if customer_id else 0
        if customer_id and points:
            # event_id của sự kiện điểm = event_id dòng ledger (docs/12 §3.3).
            self._enqueue(
                "PointsEarned",
                PointsPayload(
                    customer_id=customer_id, sale_id=sale_id, delta=points, reason="EARN"
                ),
                at,
            )
            self.lifetime[customer_id] = lifetime + points
        paid: dict[str, int] = {}
        for p in payments:
            paid[p["method"]] = paid.get(p["method"], 0) + p["amount"]
        shift.cash_from_sales += paid.get("CASH", 0)
        self.record.sales[str(sale_id)] = SaleRecord(
            sale_id=str(sale_id),
            shift_id=shift.shift_id,
            business_date=business_date.isoformat(),
            total=priced.total,
            payments=paid,
            points=points,
            customer_id=str(customer_id) if customer_id else None,
        )

    # ───────────── xả outbox (như `edge.sync.worker.run_forever`) ─────────────

    async def send_forever(self, t0: float, deadline: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        failures = 0
        batch_size = self.settings.batch_size
        while not deadline.is_set():
            if not self.producing and not self.outbox:
                # Outbox trống, không còn gì để bán: worker thật lúc này gửi heartbeat — trung tâm
                # ghi nhận "cửa hàng đã bắt kịp" (trễ đồng bộ về 0).
                with contextlib.suppress(CentralUnavailableError):
                    await self.client.push([])
                return
            now = loop.time()
            if self.offline is not None:
                self.network.online = not (self.offline[0] <= now - t0 < self.offline[1])
            due = [(eid, p) for eid, p in self.outbox.items() if p.not_before <= now][:batch_size]
            if not due:
                await asyncio.sleep(self.settings.poll_interval_seconds)
                continue
            envelopes = [p.envelope for _, p in due]
            started = time.perf_counter()
            try:
                result = await self.client.push(envelopes)
            except CentralUnavailableError as exc:
                # Lỗi đường truyền: KHÔNG đụng `attempts` của sự kiện nào.
                failures += 1
                self.stats["transport_errors"] += 1
                delay = (
                    exc.retry_after
                    if exc.retry_after is not None
                    else next_backoff(failures, max_seconds=self.settings.backoff_max_seconds)
                )
                await asyncio.sleep(delay)
                continue
            self.manifest.observe("POST /events", (time.perf_counter() - started) * 1000)
            failures = 0
            accepted = self._apply(due, result, now)
            if self.quirks.resend and accepted and self.rng.random() < self.quirks.resend_share:
                await self._resend(envelopes, accepted)
            if len(due) < batch_size:
                await asyncio.sleep(self.settings.poll_interval_seconds)

    def _apply(self, due: list[tuple[str, _Pending]], result: IngestResult, now: float) -> set[str]:
        batch = dict(due)
        accepted = {str(e) for e in result.accepted} & batch.keys()
        for eid in accepted:
            del self.outbox[eid]
            self.stats["sent"] += 1
            if eid in self.customer_events:
                ref, customer_id = self.customer_events.pop(eid)
                self.central.customers[ref] = customer_id
        handled = set(accepted)
        for rejection in result.rejected:
            eid = str(rejection.event_id)
            if eid in batch and eid not in handled:
                handled.add(eid)
                self._failure(eid, batch[eid], rejection.reason, rejection.retryable, now)
        for eid, pending in batch.items():
            if eid not in handled:
                self._failure(eid, pending, "missing_in_response", True, now)
        return accepted

    def _failure(
        self, eid: str, pending: _Pending, reason: str, retryable: bool, now: float
    ) -> None:
        """Trung tâm đã xét và từ chối — lúc này, và chỉ lúc này, mới tính một lượt thử."""
        pending.attempts += 1
        if not retryable or pending.attempts >= self.settings.max_attempts_before_dead_letter:
            del self.outbox[eid]
            self.stats["dead"] += 1
            self.record.rejected.append(
                {
                    "event_id": eid,
                    "event_type": pending.envelope["event_type"],
                    "reason": reason,
                    "attempts": pending.attempts,
                }
            )
            return
        pending.not_before = now + next_backoff(
            pending.attempts, max_seconds=self.settings.backoff_max_seconds
        )

    async def _resend(self, envelopes: list[dict[str, object]], accepted: set[str]) -> None:
        """CH-7: gửi lại nguyên lô vừa được nhận, `resend_times` lần. Idempotency phải trả
        `accepted` cho MỌI sự kiện đã nhận lần trước — thiếu là trung tâm "quên" nó đã nhận gì."""
        for _ in range(max(1, self.quirks.resend_times)):
            try:
                again = await self.client.push(envelopes)
            except CentralUnavailableError as exc:
                if exc.retry_after is not None:  # 429/503: lùi như worker thật rồi gửi tiếp
                    await asyncio.sleep(exc.retry_after)
                continue  # mất mạng giữa chừng — lần này không có gì để kiểm
            self.stats["resent_batches"] += 1
            missing = accepted - {str(e) for e in again.accepted}
            if missing:
                self.stats["resend_mismatch"] += len(missing)

    def finish(self) -> None:
        self.stats["pending"] = len(self.outbox)


async def run_virtual(
    targets: Sequence[VirtualTarget],
    plans: Mapping[str, Sequence[Intent]],
    *,
    central_url: str,
    manifest: Manifest,
    profile: Profile,
    start_date: date,
    rate: float,
    prices: Mapping[str, int],
    quirks: Quirks,
    offline_store_ids: Sequence[str] = (),
    settings: SyncSettings | None = None,
    utc_offset_minutes: int = 420,
    pii_key: str = "simulator-virtual",
    drain_timeout: float = 600.0,
    seed: int = 0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Phát lịch ý định của mọi cửa hàng ảo theo đúng giờ (vòng hở), rồi chờ xả hết outbox.

    `transport` chỉ để test: trỏ thẳng vào ASGI app của trung tâm, không qua mạng thật."""
    settings = settings or SyncSettings()
    clock = _Clock(profile, start_date, rate, timedelta(minutes=utc_offset_minutes))
    central = _Central()
    offline_real: tuple[float, float] | None = None
    if quirks.offline:
        lo, hi = quirks.offline_window(profile, manifest.days)
        offline_real = (lo / rate, hi / rate)

    stores: dict[str, _VirtualStore] = {}
    for target in targets:
        network = _Network(transport or httpx.AsyncHTTPTransport())
        stores[target.store_id] = _VirtualStore(
            target,
            client=HttpCentralClient(
                base_url=central_url,
                api_key=target.api_key,
                timeout_seconds=settings.request_timeout_seconds,
                transport=network,
            ),
            network=network,
            manifest=manifest,
            settings=settings,
            clock=clock,
            prices=prices,
            opening_cash=profile.opening_cash,
            pii_key=pii_key,
            central=central,
            quirks=quirks,
            offline=offline_real if target.store_id in offline_store_ids else None,
            seed=seed,
        )

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    cpu0, wall0 = time.process_time(), time.perf_counter()
    deadline = asyncio.Event()
    senders = [asyncio.create_task(s.send_forever(t0, deadline)) for s in stores.values()]
    schedule = sorted(
        ((it.at, n, sid, it) for sid, intents in plans.items() for n, it in enumerate(intents)),
        key=lambda x: (x[0], x[1], x[2]),
    )
    try:
        for i, (at, _, sid, intent) in enumerate(schedule):
            delay = t0 + at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif i % 50 == 0:
                manifest.scheduler_lag_s.append(-delay)
            stores[sid].apply(intent)
        for s in stores.values():
            s.producing = False
        try:
            await asyncio.wait_for(asyncio.gather(*senders), timeout=drain_timeout)
        except TimeoutError:
            deadline.set()  # phần còn lại ghi là `pending` — bộ đối soát sẽ thấy
    finally:
        deadline.set()
        for task in senders:
            task.cancel()
        await asyncio.gather(*senders, return_exceptions=True)
        for s in stores.values():
            s.finish()
            await s.client.aclose()
        wall = time.perf_counter() - wall0
        manifest.cpu_share = round((time.process_time() - cpu0) / wall, 4) if wall else None
        manifest.finished_at = datetime.now(UTC).isoformat()
