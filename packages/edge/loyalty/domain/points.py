"""Tính điểm và suy số dư — FR-L03, FR-L08, ADR-002.

Thuần: không DB, không HTTP. Hai nhóm hàm:
  - `points_earned_for` / `points_to_reverse`: bao nhiêu điểm cho một giao dịch.
  - `balance_of` / `lifetime_earned_of` / `displayed_balance`: suy trạng thái từ ledger.

## Nguyên tắc #3 — sự kiện bất biến, trạng thái suy ra được

Không có hàm nào ở đây "cập nhật số dư". Số dư là `sum(delta)`. `point_balance` ở trung
tâm chỉ là snapshot cho nhanh, và job đối soát hằng ngày kiểm rằng nó vẫn bằng tổng ledger
(INV-4, DI-1, AT-10). Nếu lệch, ledger đúng — snapshot sai.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from shared.types import Money, PointReason

if TYPE_CHECKING:
    from collections.abc import Iterable


class PointsError(ValueError):
    """Quy tắc tích điểm không hợp lệ."""


@dataclass(frozen=True, slots=True)
class EarnRule:
    """Quy tắc tích điểm — **dữ liệu**, không phải cấu hình ứng dụng.

    Nó nằm ở bảng `earn_rule` của trung tâm và có `valid_from`/`valid_to`, vì giao dịch cũ
    phải áp quy tắc cũ (docs/05 §5). Việc chọn đúng quy tắc có hiệu lực tại thời điểm mở
    đơn thuộc về use case; ở đây chỉ nhận quy tắc đã chọn.

    Mặc định 10.000đ = 1 điểm (FR-L03).
    """

    vnd_per_point: int = 10_000
    multiplier: Decimal = Decimal("1.0")

    def __post_init__(self) -> None:
        if self.vnd_per_point <= 0:
            raise PointsError(f"vnd_per_point phải > 0, nhận được {self.vnd_per_point}")
        if self.multiplier < 0:
            raise PointsError(f"multiplier không được âm, nhận được {self.multiplier}")


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Một dòng `point_ledger`. Chỉ giữ phần domain cần — không có `event_id`, `store_id`."""

    delta: int
    reason: PointReason


def points_earned_for(amount: Money, rule: EarnRule) -> int:
    """Điểm tích được từ `amount` đồng.

    ⚠️ `amount` là **`sale.total`** — số tiền khách thực trả sau chiết khấu, không phải
    `subtotal`. Tích trên `subtotal` nghĩa là thưởng điểm cho phần tiền khách không trả,
    và khách hạng cao sẽ tích nhiều hơn khách hạng thấp trên cùng số tiền bỏ ra.

    Làm tròn **xuống**: 19.999đ với quy tắc 10.000đ/điểm ra 1 điểm, không phải 2. Đây là
    hướng an toàn — cấp dư điểm là mất tiền thật và không thu lại được (thu hồi điểm đã
    tiêu thì phải xử lý số dư âm).
    """
    if amount < 0:
        raise PointsError(f"amount âm ({amount}) — dùng points_to_reverse cho trả hàng")
    base = amount // rule.vnd_per_point
    if rule.multiplier == 1:
        return int(base)
    return int((Decimal(base) * rule.multiplier).to_integral_value(rounding=ROUND_HALF_UP))


def points_to_reverse(*, earned_points: int, sale_total: Money, returned_amount: Money) -> int:
    """Điểm phải trừ khi trả hàng — FR-L08, AT-06.

    Trừ **theo tỷ lệ giá trị trả về**, không tính lại từ đầu bằng `points_earned_for`:
    tính lại sẽ sai khi quy tắc tích điểm đã thay đổi giữa lúc mua và lúc trả, và khi
    khuyến mãi x2 điểm đã hết hạn. Tỷ lệ thì luôn nhất quán với số điểm đã thực cấp.

    Trả về số **dương**; gọi viên tự đặt dấu âm khi ghi ledger.

    Trả toàn bộ đơn thì trừ đúng toàn bộ điểm đã cấp — không để sót 1 điểm vì làm tròn.
    """
    if returned_amount <= 0:
        raise PointsError(f"returned_amount phải > 0, nhận được {returned_amount}")
    if sale_total <= 0:
        raise PointsError(f"sale_total phải > 0, nhận được {sale_total}")
    if returned_amount > sale_total:
        raise PointsError(f"Trả ({returned_amount}) nhiều hơn giá trị đơn gốc ({sale_total})")
    if returned_amount == sale_total:
        return earned_points

    # Làm tròn nửa lên: nghiêng về phía thu hồi đủ điểm hơn là để sót.
    return int(
        (Decimal(earned_points) * Decimal(returned_amount) / Decimal(sale_total)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


def balance_of(entries: Iterable[LedgerEntry]) -> int:
    """Số dư = tổng mọi `delta`. Có thể ÂM nếu `RETURN_ALLOW_NEGATIVE_BALANCE=true`."""
    return sum(entry.delta for entry in entries)


def lifetime_earned_of(entries: Iterable[LedgerEntry]) -> int:
    """Tổng điểm từng tích được — cơ sở XẾP HẠNG (ràng buộc #6).

    Chỉ cộng dòng `EARN` dương. Cố ý bỏ qua `RETURN`, `REDEEM`, `EXPIRE`:

      - `REDEEM`/`EXPIRE`: tiêu điểm hoặc để điểm hết hạn không được làm tụt hạng khách.
        Đây là lý do tồn tại của việc tách `lifetime_earned` khỏi `balance` (ADR-002).
      - `RETURN`: trả hàng cũng không làm tụt hạng, theo mặc định
        `RETURN_DEMOTE_TIER_ON_RETURN=false` (docs/05 §5). Nếu cấu hình đó bật, use case
        phải trừ tường minh — hàm này giữ nguyên nghĩa "từng tích được".
    """
    return sum(entry.delta for entry in entries if entry.reason == "EARN" and entry.delta > 0)


def displayed_balance(*, central_balance: int, unsynced_deltas: Iterable[int]) -> int:
    """Số dư hiển thị cho khách = số dư trung tâm + các dòng cục bộ CHƯA đồng bộ.

    docs/03 §4.3. Không có phép cộng này thì số dư sẽ **tụt lùi** ngay trước mắt khách:
    vừa mua xong thấy +50 điểm (ledger cục bộ), rồi cửa hàng kéo số dư từ trung tâm về —
    nơi chưa nhận được sự kiện — và con số quay lại giá trị cũ.
    """
    return central_balance + sum(unsynced_deltas)
