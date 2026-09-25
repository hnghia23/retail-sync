"""Tật của dữ liệu thật — docs/18 §5. Mỗi tật là một cờ bật/tắt kèm tham số cấu hình được.

Dữ liệu quá sạch không lộ được lỗi mà dữ liệu thật sẽ lộ. Ba tật của cổng A (chế độ `virtual`):

- `offline`: một phần cửa hàng mất mạng trong một khoảng giờ CỬA HÀNG (ví dụ 18:00 ngày áp chót
  → 12:00 ngày cuối). Cửa hàng vẫn bán (tự chủ); sự kiện dồn lại rồi xả khi có mạng. Chọn ngày
  bắt đầu sao cho khoảng đó vắt qua cuối tháng là kiểm được bẫy 5 (docs/17 §4).
- `resend`: một phần lô đã được nhận bị gửi LẠI nguyên lô (CH-7). Trung tâm phải trả `accepted`
  cho mọi sự kiện, và không có gì bị ghi hai lần.
- `concurrent_customer`: khách đăng ký ở cửa hàng A mua ở cửa hàng B vào gần cùng lúc với một
  lần mua ở A (AT-04) — hai cửa hàng cộng điểm cho cùng một số dư.

Khoảng offline tính theo giờ MỞ CỬA của cửa hàng: đêm không tồn tại trong thời gian giả lập, nên
"18:00 hôm nay → 12:00 mai" là 4 giờ tối nay + 5 giờ sáng mai.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from simulator.generator import CustomerRef, Sale

if TYPE_CHECKING:
    from collections.abc import Sequence

    from simulator.generator import Intent
    from simulator.profile import Profile

__all__ = ["KNOWN", "Quirks", "mark_concurrent_customers", "offline_stores"]

KNOWN = ("offline", "resend", "concurrent_customer")


@dataclass(frozen=True, slots=True)
class Quirks:
    offline: bool = False
    #: Tỉ lệ cửa hàng bị offline (ít nhất 1 khi bật).
    offline_share: float = 0.25
    #: Ngày (0 = ngày đầu; âm = đếm từ cuối, -2 = áp chót) và giờ cửa hàng bắt đầu mất mạng.
    offline_from_day: int = -2
    offline_from_hour: float = 18.0
    #: Ngày và giờ có mạng lại. Mặc định: 12:00 ngày cuối.
    offline_to_day: int = -1
    offline_to_hour: float = 12.0
    resend: bool = False
    #: Tỉ lệ lô ĐÃ ĐƯỢC NHẬN bị gửi lại nguyên lô.
    resend_share: float = 0.1
    #: Mỗi lô bị gửi lại bao nhiêu LẦN (CH-7: 10 lần — điểm vẫn cộng đúng một lần).
    resend_times: int = 1
    concurrent_customer: bool = False
    #: Tỉ lệ đơn (của khách định danh cũ) ở mỗi cửa hàng được chuyển sang một khách của cửa
    #: hàng khác, xếp gần giờ một lần mua của khách đó ở cửa hàng nhà.
    concurrent_share: float = 0.05

    @classmethod
    def parse(cls, names: str, overrides: Sequence[str] = ()) -> Quirks:
        """`offline,resend` + `offline_share=0.5`... (tham số là tên field của lớp này)."""
        enabled = {n.strip() for n in names.split(",") if n.strip()}
        unknown = enabled - set(KNOWN)
        if unknown:
            raise ValueError(f"tật không biết: {sorted(unknown)} (có: {', '.join(KNOWN)})")
        q = cls(**dict.fromkeys(enabled, True))
        for item in overrides:
            key, sep, raw = item.partition("=")
            if not sep or key not in cls.__dataclass_fields__ or key in KNOWN:
                raise ValueError(f"--quirk-arg không hợp lệ: {item!r}")
            current = getattr(q, key)
            q = replace(q, **{key: type(current)(raw)})
        return q

    def names(self) -> list[str]:
        return [n for n in KNOWN if getattr(self, n)]

    def offline_window(self, profile: Profile, days: int) -> tuple[float, float]:
        """Khoảng offline tính bằng GIÂY GIẢ LẬP kể từ lúc mở cửa ngày đầu."""
        day_seconds = profile.day_hours * 3600

        def at(day: int, hour: float) -> float:
            d = day % days
            h = min(max(hour, profile.open_hour), profile.close_hour)
            return d * day_seconds + (h - profile.open_hour) * 3600

        start = at(self.offline_from_day, self.offline_from_hour)
        end = at(self.offline_to_day, self.offline_to_hour)
        if end <= start:
            raise ValueError("khoảng offline rỗng: giờ có mạng lại phải sau giờ mất mạng")
        return start, end


def offline_stores(store_ids: Sequence[str], quirks: Quirks, *, seed: int) -> list[str]:
    if not quirks.offline:
        return []
    rng = random.Random(f"{seed}:offline")  # noqa: S311 — tái lập theo seed, không phải bí mật
    n = max(1, round(len(store_ids) * quirks.offline_share))
    return sorted(rng.sample(list(store_ids), min(n, len(store_ids))))


def mark_concurrent_customers(
    plans: dict[str, list[Intent]], quirks: Quirks, *, seed: int
) -> dict[str, list[Intent]]:
    """Chuyển một phần đơn sang khách của cửa hàng khác, xếp gần giờ một lần mua ở cửa hàng nhà.

    Chỉ đánh dấu Ý ĐỊNH. Lúc chạy, cửa hàng B chỉ bán được cho khách của A nếu trung tâm đã nhận
    `CustomerCreated` của khách đó — ngoài đời B tra khách qua trung tâm, nên khách A chưa đồng
    bộ lên thì B không thể biết (xem `simulator.sinks.virtual`)."""
    if not quirks.concurrent_customer or len(plans) < 2:
        return plans
    rng = random.Random(f"{seed}:concurrent")  # noqa: S311
    # Lần mua của khách CŨ (đã đăng ký trước đó) ở chính cửa hàng nhà: (at, CustomerRef).
    home: dict[str, list[tuple[float, CustomerRef]]] = {
        sid: [
            (it.at, it.customer)
            for it in intents
            if isinstance(it, Sale) and it.customer is not None and not it.customer.new
        ]
        for sid, intents in plans.items()
    }
    out: dict[str, list[Intent]] = {}
    for sid, intents in plans.items():
        others = [(at, c) for other, visits in home.items() if other != sid for at, c in visits]
        if not others:
            out[sid] = intents
            continue
        others.sort(key=lambda v: v[0])
        changed: list[Intent] = []
        for it in intents:
            if (
                isinstance(it, Sale)
                and it.customer is not None
                and not it.customer.new
                and rng.random() < quirks.concurrent_share
            ):
                # Lần mua ở cửa hàng khác gần giờ nhất → cùng khách, gần cùng lúc.
                _, customer = min(others, key=lambda v: abs(v[0] - it.at))
                it = replace(it, customer=replace(customer, new=False))
            changed.append(it)
        out[sid] = changed
    return out
