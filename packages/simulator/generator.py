"""Bộ sinh ý định — thuần, tái lập theo seed (docs/18 §1, §4).

Sinh ra một lịch các **ý định** (mở ca, bán, đóng ca) cho từng cửa hàng, kèm thời điểm tính
bằng giây kể từ lúc bắt đầu chạy. Không biết gì về HTTP, DB hay giá tiền: giá do CỬA HÀNG
tính (`POST /sales/quote`), bộ sinh chỉ quyết định khách mua gì, bao nhiêu, trả bằng gì.

## Thời gian ở chế độ `edge`

Đồng hồ là đồng hồ THẬT của cửa hàng (`occurred_at` do cửa hàng đóng dấu — ADR-010 quy tắc
5). `rate` nén một ngày mở cửa (mặc định 15 giờ) thành `15h / rate` thời gian thật: `rate=60`
→ một "ngày" giả lập dài 15 phút. Nhiều ngày giả lập trong một ngày thật vẫn mang cùng
`business_date` thật — đúng, vì cửa hàng không cho khai ngày khác hôm nay/hôm qua.

## Phân bố — Poisson không thuần nhất, vòng hở

Số đơn một ngày ~ Poisson(λ), thời điểm từng đơn rút theo tỉ lệ doanh số của từng giờ. Lịch
cố định TRƯỚC khi chạy: đơn tới đúng giờ đã định dù hệ thống đang chậm (vòng hở, docs/18 §1).
"""

from __future__ import annotations

import itertools
import math
import random
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from simulator.profile import Profile

__all__ = [
    "CloseShift",
    "CustomerRef",
    "Intent",
    "OpenShift",
    "Sale",
    "plan_store",
]


@dataclass(frozen=True, slots=True)
class CustomerRef:
    """Khách của bộ giả lập. `ref` là tên nội bộ; `customer_id` thật do cửa hàng cấp lúc chạy."""

    ref: str
    new: bool
    phone: str


@dataclass(frozen=True, slots=True)
class OpenShift:
    at: float
    day: int
    shift_no: int


@dataclass(frozen=True, slots=True)
class CloseShift:
    at: float
    day: int
    shift_no: int


@dataclass(frozen=True, slots=True)
class Sale:
    at: float
    intent_id: str
    lines: tuple[tuple[str, int], ...]
    customer: CustomerRef | None
    #: "CASH" | "CARD" | "EWALLET" | "SPLIT" (tiền mặt + thẻ)
    payment: str


Intent = OpenShift | CloseShift | Sale


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth cho λ nhỏ; xấp xỉ chuẩn cho λ lớn (`exp(-λ)` tràn số dưới khi λ > ~700)."""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


class _PhoneBook:
    """SĐT duy nhất trong một lần chạy, tất định theo (seed, nonce).

    `nonce` khác nhau giữa hai lần chạy trên CÙNG một DB cửa hàng: nếu không, lần chạy thứ hai
    đăng ký lại đúng các số cũ, cửa hàng trả khách cũ (có sẵn điểm từ lần trước), và điểm kỳ
    vọng trong manifest sai. Trùng SĐT có chủ đích (C03) là một "tật" riêng, bật tường minh.
    """

    def __init__(self, rng: random.Random, nonce: int) -> None:
        self._rng = rng
        self._offset = nonce % 10**8
        self._used: set[str] = set()

    def next(self) -> str:
        while True:
            body = (self._offset + self._rng.randrange(10**8)) % 10**8
            phone = f"09{body:08d}"
            if phone not in self._used:
                self._used.add(phone)
                return phone


def plan_store(
    profile: Profile,
    *,
    store_id: str,
    catalog: Sequence[str],
    seed: int,
    nonce: int,
    days: int,
    rate: float,
    start_date: date,
    prices: Mapping[str, int] | None = None,
) -> list[Intent]:
    """Lịch ý định của MỘT cửa hàng trong `days` ngày giả lập, sắp theo `at`.

    Mỗi cửa hàng có luồng ngẫu nhiên riêng (seed + store_id): thêm/bớt một cửa hàng không làm
    đổi lịch của các cửa hàng còn lại.

    `prices` chỉ dùng để XẾP HẠNG độ phổ biến (đồ rẻ bán nhiều hơn), không dùng để tính tiền —
    tiền do cửa hàng tính. Không có `prices` thì thứ hạng là xáo ngẫu nhiên. Lần chạy thật đầu
    tiên (2026-09-23) dùng xáo ngẫu nhiên và ra trung bình 3,1 triệu/đơn, vì một mã 49 triệu có
    thể đứng đầu bảng Zipf.
    """
    if not catalog:
        raise ValueError("danh mục sản phẩm rỗng")
    if rate <= 0:
        raise ValueError("rate phải dương")

    # PRNG tất định là CHỦ ĐÍCH (tái lập theo seed, docs/18 §1) — đây là dữ liệu giả lập,
    # không phải bí mật.
    rng = random.Random(f"{seed}:{store_id}")  # noqa: S311
    phones = _PhoneBook(random.Random(f"{seed}:{store_id}:phone"), nonce)  # noqa: S311

    ranked = list(catalog)
    rng.shuffle(ranked)  # thứ hạng Zipf không phụ thuộc thứ tự file CSV
    if prices:
        noise = profile.price_rank_noise
        keyed = {p: prices.get(p, 0) * rng.lognormvariate(0, noise) for p in ranked}
        ranked.sort(key=keyed.__getitem__)
    cum_weights = list(
        itertools.accumulate(1 / (rank**profile.zipf_s) for rank in range(1, len(ranked) + 1))
    )
    hour_cum = list(itertools.accumulate(profile.hour_weights()))
    methods = [m for m, _ in profile.payment_mix]
    method_weights = [w for _, w in profile.payment_mix]

    day_seconds = profile.day_hours * 3600  # giây giả lập của một ngày mở cửa
    shift_len = day_seconds / profile.shifts_per_day
    guard = shift_len * profile.changeover_guard
    pool: list[CustomerRef] = []

    def real(day: int, t: float) -> float:
        return (day * day_seconds + t) / rate

    intents: list[Intent] = []
    for day in range(days):
        weekday = (start_date + timedelta(days=day)).weekday()
        lam = profile.sales_per_store_day * (profile.weekend_factor if weekday >= 5 else 1.0)
        times: list[float] = []
        for _ in range(_poisson(rng, lam)):
            hour_idx = min(bisect_left(hour_cum, rng.random() * hour_cum[-1]), len(hour_cum) - 1)
            t = hour_idx * 3600 + rng.random() * 3600
            shift_no = min(int(t // shift_len), profile.shifts_per_day - 1)
            lo, hi = shift_no * shift_len + guard, (shift_no + 1) * shift_len - guard
            times.append(min(max(t, lo), hi))
        times.sort()

        for shift_no in range(profile.shifts_per_day):
            intents.append(
                OpenShift(at=real(day, shift_no * shift_len), day=day, shift_no=shift_no)
            )
            intents.append(
                CloseShift(
                    at=real(day, (shift_no + 1) * shift_len - guard / 2), day=day, shift_no=shift_no
                )
            )

        for i, t in enumerate(times):
            customer: CustomerRef | None = None
            if rng.random() < profile.identified_share:
                p_new = profile.new_customer_share / profile.identified_share
                if not pool or rng.random() < p_new:
                    customer = CustomerRef(
                        ref=f"{store_id}-c{len(pool):05d}", new=True, phone=phones.next()
                    )
                    pool.append(CustomerRef(customer.ref, new=False, phone=customer.phone))
                else:
                    customer = rng.choice(pool)

            n_lines = 1 + _poisson(rng, profile.lines_extra_mean)
            basket: dict[str, int] = {}
            for product in rng.choices(ranked, cum_weights=cum_weights, k=n_lines):
                qty = (
                    rng.randint(2, profile.qty_multi_max)
                    if rng.random() < profile.qty_multi_share
                    else 1
                )
                basket[product] = basket.get(product, 0) + qty

            intents.append(
                Sale(
                    at=real(day, t),
                    intent_id=f"{store_id}-d{day}-{i:05d}",
                    lines=tuple(basket.items()),
                    customer=customer,
                    payment=rng.choices(methods, weights=method_weights, k=1)[0],
                )
            )

    # Cùng thời điểm: đóng ca trước mở ca, mở ca trước bán — thứ tự để sắp xếp ổn định.
    order = {CloseShift: 0, OpenShift: 1, Sale: 2}
    intents.sort(key=lambda it: (it.at, order[type(it)]))
    return intents
