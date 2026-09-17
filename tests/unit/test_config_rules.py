"""Quy tắc nghiệp vụ là **config**, không hardcode — docs/05-data-model.md §5.

Hai giá trị dưới đây có hệ quả CHECK constraint, nên test này cũng là lời nhắc: đổi
chúng sau khi đã có dữ liệu là một migration, không phải sửa biến môi trường.
"""

from __future__ import annotations

from shared.config import PricingRules, ReturnRules


def test_pricing_defaults_match_env_example() -> None:
    rules = PricingRules(_env_file=None)
    assert rules.discount_stacking == "additive"
    assert rules.rounding == "to_dong_last_line"  # ⚠️ hệ quả CHECK (subtotal = total + discount)
    assert rules.promotion_applies_at == "order_open"


def test_return_defaults_match_env_example() -> None:
    rules = ReturnRules(_env_file=None)
    assert rules.allow_cross_store is False
    assert rules.demote_tier_on_return is False  # ràng buộc #6 — trả hàng không làm tụt hạng
    assert rules.allow_negative_balance is True  # ⚠️ hệ quả CHECK (balance >= 0)
