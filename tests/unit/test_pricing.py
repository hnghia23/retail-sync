"""Tính tiền — docs/16 §1, Q-B1 (cộng gộp) và Q-B2 (làm tròn).

Không cần Docker, chạy < 1s. Mục tiêu của bộ test này không phải "phủ hết dòng" mà là
**khóa chặt INV-1**: mọi tổ hợp cấu hình × mọi giỏ hàng đều phải giữ
`subtotal = total + discount_tier + discount_promo`, vì đó là `CHECK` constraint ở DB.
"""

from __future__ import annotations

import itertools
from typing import Literal

import pytest

from edge.pos.domain.pricing import CartLine, PricingError, price_cart
from shared.config import PricingRules

ALL_RULES = [
    PricingRules(discount_stacking=s, rounding=r, promotion_applies_at="order_open")
    for s, r in itertools.product(["additive", "multiplicative"], ["to_dong_last_line", "per_line"])
]


DiscountStacking = Literal["additive", "multiplicative"]
Rounding = Literal["to_dong_last_line", "per_line"]


def _rules(
    stacking: DiscountStacking = "additive", rounding: Rounding = "to_dong_last_line"
) -> PricingRules:
    return PricingRules(
        discount_stacking=stacking, rounding=rounding, promotion_applies_at="order_open"
    )


# ═══════════════════════ Đường chính ═══════════════════════


def test_simple_cart_without_discount() -> None:
    result = price_cart(
        [CartLine("SKU-001", 150_000, 2), CartLine("SKU-002", 100_000, 1)], rules=_rules()
    )
    assert result.subtotal == 400_000
    assert result.total == 400_000
    assert [line.line_no for line in result.lines] == [1, 2]


def test_tier_discount_only() -> None:
    """Khách GOLD 5%: 400.000 → giảm 20.000."""
    result = price_cart([CartLine("SKU-001", 400_000, 1)], rules=_rules(), tier_discount_pct=5)
    assert result.discount_tier == 20_000
    assert result.discount_promo == 0
    assert result.total == 380_000


def test_additive_stacking_adds_percentages() -> None:
    """Q-B1 `additive`: 5% hạng + 10% khuyến mãi = giảm 15% trên tạm tính."""
    result = price_cart(
        [CartLine("SKU-001", 1_000_000, 1)],
        rules=_rules("additive"),
        tier_discount_pct=5,
        promo_discount_pct=10,
    )
    assert result.discount_tier == 50_000
    assert result.discount_promo == 100_000
    assert result.total == 850_000


def test_multiplicative_stacking_applies_in_sequence() -> None:
    """Q-B1 `multiplicative`: giảm 5%, rồi giảm tiếp 10% trên phần CÒN LẠI.

    1.000.000 → 950.000 → 855.000. Tổng chiết khấu 145.000, ít hơn additive 5.000 —
    khác biệt này là lý do cấu hình phải được chốt trước khi chạy thật.
    """
    result = price_cart(
        [CartLine("SKU-001", 1_000_000, 1)],
        rules=_rules("multiplicative"),
        tier_discount_pct=5,
        promo_discount_pct=10,
    )
    assert result.total == 855_000
    assert result.discount_tier == 50_000
    assert result.discount_promo == 95_000


def test_multiplicative_discounts_less_than_additive() -> None:
    """Quan hệ này phải đúng với mọi %, không chỉ với ví dụ ở trên."""
    lines = [CartLine("SKU-001", 777_777, 3)]
    add = price_cart(lines, rules=_rules("additive"), tier_discount_pct=8, promo_discount_pct=12)
    mul = price_cart(
        lines, rules=_rules("multiplicative"), tier_discount_pct=8, promo_discount_pct=12
    )
    assert mul.total > add.total


# ═══════════════════════ INV-1: bất biến phải giữ ở MỌI cấu hình ═══════════════════════


@pytest.mark.parametrize("rules", ALL_RULES, ids=lambda r: f"{r.discount_stacking}/{r.rounding}")
@pytest.mark.parametrize("tier_pct,promo_pct", [(0, 0), (3, 0), (0, 7), (5, 10), (8, 12), (33, 33)])
def test_inv1_holds_for_every_configuration(
    rules: PricingRules, tier_pct: int, promo_pct: int
) -> None:
    """Giỏ hàng có số lẻ để ép làm tròn phải hoạt động."""
    result = price_cart(
        [
            CartLine("SKU-001", 333_333, 3),
            CartLine("SKU-002", 19_999, 7),
            CartLine("SKU-003", 1, 1),
        ],
        rules=rules,
        tier_discount_pct=tier_pct,
        promo_discount_pct=promo_pct,
    )
    assert result.subtotal == result.total + result.discount_tier + result.discount_promo
    assert result.total >= 0


@pytest.mark.parametrize("rules", ALL_RULES, ids=lambda r: f"{r.discount_stacking}/{r.rounding}")
def test_subtotal_always_equals_sum_of_lines(rules: PricingRules) -> None:
    """INV-3 ở tầng domain: DB cũng kiểm lại bằng constraint trigger."""
    lines = [CartLine("SKU-001", 12_345, 2), CartLine("SKU-002", 67_890, 3)]
    result = price_cart(lines, rules=rules, tier_discount_pct=5, promo_discount_pct=5)
    assert result.subtotal == sum(line.line_total for line in result.lines)


# ═══════════════════════ Làm tròn ═══════════════════════


def test_rounding_is_half_up_not_bankers() -> None:
    """`round()` của Python làm tròn nửa về số CHẴN: round(2.5) == 2.

    Với tiền thì đó là hành vi bất ngờ. 50đ × 5% = 2,5đ phải ra 3đ.
    """
    result = price_cart([CartLine("SKU-001", 50, 1)], rules=_rules(), tier_discount_pct=5)
    assert result.discount_tier == 3


def test_per_line_and_whole_order_rounding_may_differ() -> None:
    """Q-B2 có hệ quả thật, không chỉ là sở thích.

    Ba dòng 33.333đ, giảm 5%: làm tròn từng dòng cho kết quả khác làm tròn trên tổng.
    Đây chính là lý do cấu hình này phải chốt MỘT giá trị từ migration đầu (docs/05 §5).
    """
    lines = [CartLine("SKU-001", 33_333, 1)] * 3
    whole = price_cart(lines, rules=_rules(rounding="to_dong_last_line"), tier_discount_pct=5)
    per_line = price_cart(lines, rules=_rules(rounding="per_line"), tier_discount_pct=5)
    # Trên tổng:    round(99.999 × 5%) = round(4.999,95) = 5.000
    # Từng dòng: 3 × round(33.333 × 5%) = 3 × round(1.666,65) = 3 × 1.667 = 5.001
    assert whole.discount_tier == 5_000
    assert per_line.discount_tier == 5_001
    assert whole.subtotal == per_line.subtotal
    # Lệch 1đ trên một đơn nhỏ. Ở quy mô nghìn đơn/ngày × nghìn cửa hàng thì đây là khoản
    # chênh lệch có thật giữa hai cách cấu hình — và là lý do nó phải chốt trước khi chạy.


# ═══════════════════════ Đầu vào sai phải bị chặn ═══════════════════════


def test_empty_cart_is_rejected() -> None:
    """docs/13 §3: giỏ rỗng → 400, chưa insert gì."""
    with pytest.raises(PricingError, match="rỗng"):
        price_cart([], rules=_rules())


def test_zero_quantity_line_is_rejected() -> None:
    with pytest.raises(PricingError, match="quantity = 0"):
        CartLine("SKU-001", 100_000, 0)


def test_negative_unit_price_is_rejected() -> None:
    with pytest.raises(PricingError, match="unit_price âm"):
        CartLine("SKU-001", -1, 1)


@pytest.mark.parametrize("tier_pct,promo_pct", [(101, 0), (0, 101), (-1, 0)])
def test_percentage_out_of_range_is_rejected(tier_pct: int, promo_pct: int) -> None:
    with pytest.raises(PricingError, match=r"\[0, 100\]"):
        price_cart(
            [CartLine("SKU-001", 100_000, 1)],
            rules=_rules(),
            tier_discount_pct=tier_pct,
            promo_discount_pct=promo_pct,
        )


def test_additive_discount_over_100_percent_is_rejected() -> None:
    """60% + 60% cộng gộp = 120%: chặn ở đây thay vì để `total` âm rồi vỡ CHECK ở DB."""
    with pytest.raises(PricingError, match="vượt 100%"):
        price_cart(
            [CartLine("SKU-001", 100_000, 1)],
            rules=_rules("additive"),
            tier_discount_pct=60,
            promo_discount_pct=60,
        )


def test_multiplicative_never_exceeds_subtotal() -> None:
    """Ngược lại, áp lần lượt thì không bao giờ vượt — kể cả 100% + 100%."""
    result = price_cart(
        [CartLine("SKU-001", 100_000, 1)],
        rules=_rules("multiplicative"),
        tier_discount_pct=100,
        promo_discount_pct=100,
    )
    assert result.total == 0
