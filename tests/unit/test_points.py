"""Tính điểm và xếp hạng — docs/16 §1, FR-L03/L04/L05/L08, ràng buộc #6.

Trọng tâm: **xếp hạng phải dùng `lifetime_earned`, không phải `balance`.** Đây là loại lỗi
không gây exception, không làm fail test khác, và chỉ lộ ra khi một khách VIP bị tụt hạng
sau khi tiêu điểm — lúc đó đã mất uy tín với khách.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from edge.loyalty.domain.points import (
    EarnRule,
    LedgerEntry,
    PointsError,
    balance_of,
    displayed_balance,
    lifetime_earned_of,
    points_earned_for,
    points_to_reverse,
)
from edge.loyalty.domain.tier import (
    DEFAULT_TIER_RULES,
    TierError,
    TierRule,
    resolve_tier,
    tier_discount_pct,
)

# ═══════════════════════ Tích điểm ═══════════════════════


def test_default_rule_is_ten_thousand_dong_per_point() -> None:
    """FR-L03 mặc định: 10.000đ = 1 điểm."""
    assert points_earned_for(530_000, EarnRule()) == 53


def test_earning_rounds_down() -> None:
    """19.999đ ra 1 điểm, không phải 2.

    Hướng làm tròn là quyết định nghiệp vụ, không phải chi tiết kỹ thuật: cấp dư điểm là
    mất tiền thật và không thu lại được sạch sẽ.
    """
    assert points_earned_for(19_999, EarnRule()) == 1
    assert points_earned_for(9_999, EarnRule()) == 0


def test_configurable_rate() -> None:
    """Quy tắc là DỮ LIỆU, không hardcode — cửa hàng đổi tỷ lệ không cần sửa code."""
    assert points_earned_for(100_000, EarnRule(vnd_per_point=20_000)) == 5


def test_multiplier_for_campaign() -> None:
    """FR-L12: chiến dịch x2 điểm cuối tuần."""
    assert points_earned_for(530_000, EarnRule(multiplier=Decimal("2"))) == 106
    assert points_earned_for(530_000, EarnRule(multiplier=Decimal("1.5"))) == 80  # 53 × 1.5 = 79.5


def test_negative_amount_is_rejected() -> None:
    """Trả hàng phải đi qua `points_to_reverse`, không phải tích điểm âm."""
    with pytest.raises(PointsError, match="points_to_reverse"):
        points_earned_for(-1, EarnRule())


@pytest.mark.parametrize("bad_rule", [{"vnd_per_point": 0}, {"vnd_per_point": -1}])
def test_invalid_earn_rule_is_rejected(bad_rule: dict[str, int]) -> None:
    with pytest.raises(PointsError):
        EarnRule(**bad_rule)  # type: ignore[arg-type]


# ═══════════════════════ Trả hàng — AT-06 ═══════════════════════


def test_full_return_reverses_all_points_exactly() -> None:
    """Không được sót 1 điểm vì làm tròn. Trả hết = thu hồi hết."""
    assert points_to_reverse(earned_points=53, sale_total=530_000, returned_amount=530_000) == 53


def test_partial_return_is_proportional() -> None:
    """Cổng tuần 2: trả 2 trong 5 sản phẩm, điểm trừ đúng tỷ lệ.

    Đơn 500.000đ tích 50 điểm; trả 200.000đ (2/5) → thu hồi 20 điểm.
    """
    assert points_to_reverse(earned_points=50, sale_total=500_000, returned_amount=200_000) == 20


def test_reverse_uses_ratio_not_recomputation() -> None:
    """Tại sao dùng tỷ lệ chứ không tính lại bằng `points_earned_for`.

    Đơn gốc chạy chiến dịch x2 nên 100.000đ được 20 điểm. Trả một nửa: thu hồi 10 điểm.
    Tính lại theo quy tắc hiện tại (đã hết chiến dịch) sẽ chỉ thu hồi 5 — khách giữ lại 10
    điểm cho hàng đã trả.
    """
    assert points_to_reverse(earned_points=20, sale_total=100_000, returned_amount=50_000) == 10


def test_returning_more_than_original_is_rejected() -> None:
    with pytest.raises(PointsError, match="nhiều hơn giá trị đơn gốc"):
        points_to_reverse(earned_points=50, sale_total=500_000, returned_amount=600_000)


@pytest.mark.parametrize("returned,total", [(0, 500_000), (-1, 500_000), (100, 0)])
def test_non_positive_amounts_are_rejected(returned: int, total: int) -> None:
    with pytest.raises(PointsError):
        points_to_reverse(earned_points=50, sale_total=total, returned_amount=returned)


# ═══════════════════════ Suy trạng thái từ ledger (ADR-002) ═══════════════════════


def test_balance_is_sum_of_deltas() -> None:
    entries = [
        LedgerEntry(50, "EARN"),
        LedgerEntry(30, "EARN"),
        LedgerEntry(-20, "REDEEM"),
        LedgerEntry(-10, "RETURN"),
    ]
    assert balance_of(entries) == 50


def test_balance_may_go_negative() -> None:
    """`RETURN_ALLOW_NEGATIVE_BALANCE=true`: tiêu điểm rồi trả hàng thì số dư âm là ĐÚNG.

    Đây cũng là lý do `point_balance` không có `CHECK (balance >= 0)`.
    """
    assert (
        balance_of(
            [LedgerEntry(50, "EARN"), LedgerEntry(-50, "REDEEM"), LedgerEntry(-50, "RETURN")]
        )
        == -50
    )


def test_lifetime_earned_ignores_spending_and_returns() -> None:
    """⚠️ Bất biến quan trọng nhất của module này (ràng buộc #6).

    Khách tích 8.000, tiêu 7.900, trả hàng mất thêm 100: `balance` còn 0 nhưng
    `lifetime_earned` vẫn là 8.000 — khách vẫn là GOLD.
    """
    entries = [
        LedgerEntry(8_000, "EARN"),
        LedgerEntry(-7_900, "REDEEM"),
        LedgerEntry(-100, "RETURN"),
        LedgerEntry(-50, "EXPIRE"),
    ]
    assert balance_of(entries) == -50
    assert lifetime_earned_of(entries) == 8_000
    assert resolve_tier(lifetime_earned_of(entries)).tier == "GOLD"


def test_tier_would_be_wrong_if_computed_from_balance() -> None:
    """Test này tồn tại để mô tả CHÍNH XÁC lỗi mà ràng buộc #6 ngăn chặn.

    Nếu ai đó truyền `balance` thay vì `lifetime_earned`, khách GOLD tụt về BRONZE.
    """
    entries = [LedgerEntry(8_000, "EARN"), LedgerEntry(-7_900, "REDEEM")]
    assert resolve_tier(lifetime_earned_of(entries)).tier == "GOLD"
    assert resolve_tier(balance_of(entries)).tier == "BRONZE"  # ← lỗi nếu dùng balance


def test_displayed_balance_includes_unsynced_local_entries() -> None:
    """docs/03 §4.3 — tránh số dư TỤT LÙI trước mắt khách.

    Trung tâm mới biết 100 điểm; cửa hàng vừa ghi thêm 53 chưa gửi. Hiển thị 153.
    """
    assert displayed_balance(central_balance=100, unsynced_deltas=[53]) == 153
    assert displayed_balance(central_balance=100, unsynced_deltas=[]) == 100


# ═══════════════════════ Xếp hạng ═══════════════════════


@pytest.mark.parametrize(
    "lifetime,expected",
    [
        (0, "BRONZE"),
        (999, "BRONZE"),
        (1_000, "SILVER"),
        (4_999, "SILVER"),
        (5_000, "GOLD"),
        (19_999, "GOLD"),
        (20_000, "PLATINUM"),
        (10**9, "PLATINUM"),
    ],
)
def test_tier_thresholds(lifetime: int, expected: str) -> None:
    """AT-09: đạt đủ ngưỡng thì lên hạng ngay tại mốc, không phải vượt mốc."""
    assert resolve_tier(lifetime).tier == expected


def test_tier_discount_matches_rule() -> None:
    """FR-L05: % giảm giá đi kèm hạng."""
    assert resolve_tier(5_000).discount_pct == 5
    assert tier_discount_pct("PLATINUM") == 8


def test_unknown_tier_degrades_to_zero_discount() -> None:
    """Trung tâm thêm hạng mới, cửa hàng chưa kéo quy tắc về.

    Trả 0% thay vì nổ: bán hụt chiết khấu thì khiếu nại và bù được; chặn đứng quầy thì
    vi phạm nguyên tắc kiến trúc #1 (cửa hàng phải bán được hàng).
    """
    assert tier_discount_pct("DIAMOND") == 0  # type: ignore[arg-type]


def test_rules_are_configurable() -> None:
    """Ngưỡng và % là dữ liệu đồng bộ từ trung tâm, không phải hằng số trong code."""
    custom = (TierRule("BRONZE", 0, 0), TierRule("GOLD", 100, 20))
    assert resolve_tier(150, custom).tier == "GOLD"
    assert resolve_tier(150, custom).discount_pct == 20


def test_rules_without_zero_threshold_are_rejected() -> None:
    """Lỗi cấu hình phải nổ, không được âm thầm cho khách hạng cao nhất."""
    with pytest.raises(TierError, match="thiếu mốc 0 điểm"):
        resolve_tier(50, (TierRule("SILVER", 1_000, 3),))


def test_negative_lifetime_is_rejected() -> None:
    """`lifetime_earned` âm là dữ liệu hỏng — DB cũng có CHECK cho cột này."""
    with pytest.raises(TierError):
        resolve_tier(-1)


def test_default_rules_are_sane() -> None:
    """Bộ mặc định phải phủ từ 0 và có ngưỡng tăng dần."""
    thresholds = [r.min_lifetime_points for r in DEFAULT_TIER_RULES]
    assert thresholds == sorted(thresholds)
    assert thresholds[0] == 0
