"""Xếp hạng khách — FR-L04, FR-L05, ràng buộc #6.

Thuần. Quy tắc hạng là **dữ liệu** (bảng `tier_rule`, đồng bộ xuống `tier_rule_cache`),
không phải hằng số trong code: ngưỡng và % giảm giá phải chỉnh được theo thực tế khi tích
hợp (CLAUDE.md §Ưu tiên số 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from shared.types import Tier

if TYPE_CHECKING:
    from collections.abc import Sequence


class TierError(ValueError):
    """Bộ quy tắc hạng không hợp lệ."""


#: Hạng thấp nhất — dùng cho khách vãng lai và khi chưa tra được hồ sơ (docs/13 §4).
BASE_TIER: Tier = "BRONZE"


@dataclass(frozen=True, slots=True)
class TierRule:
    """Một dòng `tier_rule`: đạt `min_lifetime_points` thì lên `tier`, được giảm `discount_pct`%."""

    tier: Tier
    min_lifetime_points: int
    discount_pct: int

    def __post_init__(self) -> None:
        if self.min_lifetime_points < 0:
            raise TierError(f"{self.tier}: min_lifetime_points không được âm")
        if not 0 <= self.discount_pct <= 100:
            raise TierError(f"{self.tier}: discount_pct phải trong [0, 100]")


#: Bộ mặc định khi cửa hàng chưa đồng bộ được `tier_rule_cache`. Cố ý để % giảm giá thấp:
#: bán hụt chiết khấu còn sửa được bằng phiếu bù, cấp thừa chiết khấu thì không đòi lại được.
DEFAULT_TIER_RULES: tuple[TierRule, ...] = (
    TierRule("BRONZE", 0, 0),
    TierRule("SILVER", 1_000, 3),
    TierRule("GOLD", 5_000, 5),
    TierRule("PLATINUM", 20_000, 8),
)


def resolve_tier(lifetime_earned: int, rules: Sequence[TierRule] = DEFAULT_TIER_RULES) -> TierRule:
    """Hạng ứng với `lifetime_earned`.

    ⚠️ Tham số là `lifetime_earned`, **không phải `balance`** (ràng buộc #6). Ký hiệu này
    cố ý không nhận `balance` để không ai truyền nhầm: hai số cùng kiểu `int`, nên trình
    kiểm kiểu không cứu được — chỉ có tên tham số và test mới cứu được.

    Chọn hạng CAO NHẤT mà khách đạt ngưỡng. Luôn trả về một quy tắc: nếu bộ quy tắc không
    có mốc 0 điểm thì đó là lỗi cấu hình, chặn ngay thay vì âm thầm cho khách hạng cao.
    """
    if lifetime_earned < 0:
        raise TierError(f"lifetime_earned không được âm ({lifetime_earned})")
    if not rules:
        raise TierError("Bộ quy tắc hạng rỗng")

    eligible = [r for r in rules if lifetime_earned >= r.min_lifetime_points]
    if not eligible:
        raise TierError(
            f"Không có hạng nào cho {lifetime_earned} điểm — bộ quy tắc thiếu mốc 0 điểm"
        )
    return max(eligible, key=lambda r: r.min_lifetime_points)


def tier_discount_pct(tier: Tier, rules: Sequence[TierRule] = DEFAULT_TIER_RULES) -> int:
    """% giảm giá của một hạng đã biết.

    Dùng khi hạng lấy từ bản sao cục bộ (`customer_local.tier`) thay vì tính lại từ điểm —
    trường hợp thường gặp nhất ở quầy, vì `lifetime_earned` có thể chưa đồng bộ.

    Hạng lạ (trung tâm thêm hạng mới, cửa hàng chưa kéo quy tắc về) → trả 0%, không nổ.
    Bán hụt chiết khấu thì khách khiếu nại và sửa được; chặn đứng quầy thì không.
    """
    for rule in rules:
        if rule.tier == tier:
            return rule.discount_pct
    return 0
