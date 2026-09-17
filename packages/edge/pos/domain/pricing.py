"""Tính tiền — FR-P05, docs/05 §5.

Thuần: không DB, không HTTP, không đồng hồ. Đây là lõi nghiệp vụ và là nơi test kỹ nhất
(roadmap tuần 1 ngày 3, cổng tuần 1 yêu cầu coverage > 80% và chạy không cần Docker).

## Một quyết định định hình cả module

`total` **luôn được suy ra bằng phép trừ**, không bao giờ tính độc lập:

    total = subtotal - discount_tier - discount_promo

Nhờ vậy bất biến INV-1 (`subtotal = total + discount_tier + discount_promo`) đúng **theo
cấu trúc**, không phụ thuộc vào cách làm tròn. Đây không phải chi tiết phong cách: INV-1
là `CHECK` constraint ở DB, nên một đồng lệch do làm tròn không làm sai số liệu — nó làm
**giao dịch bị từ chối** (docs/05 §5). Cách duy nhất để chuyện đó không bao giờ xảy ra là
không có hai đường tính `total` khác nhau.

Vì vậy `PricingRules.rounding` chỉ ảnh hưởng tới cách tính *chiết khấu*, không ảnh hưởng
tới việc INV-1 có giữ được hay không.

## Tiền tệ

`bigint` đơn vị đồng, số nguyên suốt. Không `float`, không `Decimal` — phép chia duy nhất
là nhân phần trăm, và nó làm tròn tường minh bằng số nguyên.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from shared.types import Money

if TYPE_CHECKING:
    from shared.config import PricingRules


class PricingError(ValueError):
    """Giỏ hàng hoặc quy tắc không hợp lệ → `400` ở tầng API (docs/13 §3)."""


@dataclass(frozen=True, slots=True)
class CartLine:
    """Một dòng trong giỏ. `unit_price` do gọi viên lấy từ `product_cache` và ĐÓNG BĂNG.

    Domain cố ý không tra giá: giá là dữ liệu có hiệu lực theo thời gian (docs/05 §5), việc
    chọn đúng giá tại thời điểm mở đơn thuộc về use case, không thuộc về phép tính.
    """

    product_id: str
    unit_price: Money
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity == 0:
            raise PricingError(f"quantity = 0 cho {self.product_id} — dòng vô nghĩa")
        if self.unit_price < 0:
            raise PricingError(f"unit_price âm cho {self.product_id}")

    @property
    def line_total(self) -> Money:
        return self.unit_price * self.quantity


@dataclass(frozen=True, slots=True)
class PricedLine:
    line_no: int
    product_id: str
    quantity: int
    unit_price: Money
    line_total: Money


@dataclass(frozen=True, slots=True)
class PricedSale:
    """Kết quả tính tiền. Ánh xạ 1-1 sang các cột của bảng `sale`."""

    lines: tuple[PricedLine, ...]
    subtotal: Money
    discount_tier: Money
    discount_promo: Money
    total: Money

    def __post_init__(self) -> None:
        # Lưới an toàn cuối cùng cho INV-1. Nếu nó nổ, lỗi nằm trong chính module này —
        # tốt hơn nhiều so với việc để Postgres từ chối giao dịch ở tầng dưới.
        if self.subtotal != self.total + self.discount_tier + self.discount_promo:
            raise PricingError(
                "INV-1 vi phạm trong tính tiền: "
                f"subtotal={self.subtotal} total={self.total} "
                f"discount_tier={self.discount_tier} discount_promo={self.discount_promo}"
            )


def _pct_of(amount: Money, pct: int) -> Money:
    """`pct`% của `amount`, làm tròn nửa lên, bằng số nguyên.

    Không dùng `round()` của Python: nó làm tròn nửa về số chẵn (banker's rounding), nên
    2.5 → 2. Với tiền thì đó là hành vi bất ngờ và khó giải thích cho kế toán.
    """
    return (amount * pct + 50) // 100


def _validate_pct(name: str, pct: int) -> None:
    if not 0 <= pct <= 100:
        raise PricingError(f"{name} phải trong [0, 100], nhận được {pct}")


def price_cart(
    lines: list[CartLine],
    *,
    rules: PricingRules,
    tier_discount_pct: int = 0,
    promo_discount_pct: int = 0,
) -> PricedSale:
    """Tính tiền một giỏ hàng.

    `tier_discount_pct` đọc theo hạng khách **tại lúc mở đơn** — hạng không đổi giữa chừng
    (FR-L05). `promo_discount_pct` là kết quả đã diễn giải từ `promotion.rule`; module này
    không đọc jsonb.
    """
    if not lines:
        raise PricingError("Giỏ hàng rỗng")
    _validate_pct("tier_discount_pct", tier_discount_pct)
    _validate_pct("promo_discount_pct", promo_discount_pct)

    priced = tuple(
        PricedLine(
            line_no=i,
            product_id=line.product_id,
            quantity=line.quantity,
            unit_price=line.unit_price,
            line_total=line.line_total,
        )
        for i, line in enumerate(lines, start=1)
    )
    subtotal = sum(line.line_total for line in priced)

    discount_tier, discount_promo = _split_discounts(
        [line.line_total for line in priced],
        subtotal=subtotal,
        rules=rules,
        tier_pct=tier_discount_pct,
        promo_pct=promo_discount_pct,
    )

    total = subtotal - discount_tier - discount_promo
    if total < 0:
        raise PricingError(
            f"Chiết khấu ({discount_tier + discount_promo}) vượt quá tạm tính ({subtotal})"
        )

    return PricedSale(
        lines=priced,
        subtotal=subtotal,
        discount_tier=discount_tier,
        discount_promo=discount_promo,
        total=total,
    )


def _split_discounts(
    line_totals: list[Money],
    *,
    subtotal: Money,
    rules: PricingRules,
    tier_pct: int,
    promo_pct: int,
) -> tuple[Money, Money]:
    """Tính (chiết khấu hạng, chiết khấu khuyến mãi) theo `rules`.

    Bốn tổ hợp của hai cấu hình (Q-B1 × Q-B2), và cả bốn đều phải trả về hai số nguyên
    cộng lại không vượt `subtotal`.
    """
    if rules.discount_stacking == "additive":
        # Hai chiết khấu tính độc lập trên cùng tạm tính rồi cộng lại.
        # Dễ giải thích cho khách nhất: "giảm 5% hạng + 10% khuyến mãi = giảm 15%".
        if rules.rounding == "per_line":
            tier = sum(_pct_of(lt, tier_pct) for lt in line_totals)
            promo = sum(_pct_of(lt, promo_pct) for lt in line_totals)
        else:
            tier = _pct_of(subtotal, tier_pct)
            promo = _pct_of(subtotal, promo_pct)

        # Cộng gộp có thể vượt 100% nếu cấu hình sai; chặn ở đây thay vì để total âm.
        if tier + promo > subtotal:
            raise PricingError(
                f"Cộng gộp additive vượt 100%: {tier_pct}% + {promo_pct}% trên {subtotal}"
            )
        return tier, promo

    # multiplicative: áp lần lượt — giảm 5% rồi giảm tiếp 10% trên phần còn lại.
    # Tổng chiết khấu luôn NHỎ HƠN additive, nên không bao giờ vượt subtotal.
    def _remaining(amount: Money) -> Money:
        after_tier = amount - _pct_of(amount, tier_pct)
        return after_tier - _pct_of(after_tier, promo_pct)

    if rules.rounding == "per_line":
        tier = sum(_pct_of(lt, tier_pct) for lt in line_totals)
        total_discount = subtotal - sum(_remaining(lt) for lt in line_totals)
    else:
        tier = _pct_of(subtotal, tier_pct)
        total_discount = subtotal - _remaining(subtotal)

    # Phần dư gán cho khuyến mãi: chiết khấu hạng được tính trước nên nó là số "sạch",
    # còn sai số làm tròn của bước thứ hai dồn vào đây. Cách chia này giữ cho
    # `discount_tier` khớp đúng % hiển thị trên hóa đơn.
    return tier, total_discount - tier
