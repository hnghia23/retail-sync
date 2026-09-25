"""Hai bên ranh giới edge↔central phải cùng một định nghĩa — docs/05 §2.

`import-linter` cấm edge và central import lẫn nhau (ADR-004), nên cùng một quy tắc buộc
phải viết ở hai nơi. Test là thứ duy nhất ngăn chúng trôi xa nhau âm thầm: cửa hàng offline
xếp khách một hạng, trung tâm xếp một hạng khác, và không ai thấy lỗi nào.
"""

from __future__ import annotations

import pytest

from central.ingest.handlers import lifetime_contribution
from central.ops.seed import DEFAULT_TIER_RULES as CENTRAL_TIERS
from central.ops.seed import DEFAULT_VND_PER_POINT
from edge.loyalty.domain.points import EarnRule, LedgerEntry, lifetime_earned_of
from edge.loyalty.domain.tier import DEFAULT_TIER_RULES as EDGE_TIERS
from shared.config import ReturnRules


def test_central_seed_tier_rules_match_edge_fallback() -> None:
    edge = tuple((r.tier, r.min_lifetime_points, r.discount_pct) for r in EDGE_TIERS)
    assert edge == CENTRAL_TIERS


def test_central_seed_earn_rule_matches_edge_default() -> None:
    assert EarnRule().vnd_per_point == DEFAULT_VND_PER_POINT


@pytest.mark.parametrize(
    ("delta", "reason"),
    [
        (50, "EARN"),
        (-20, "RETURN"),
        (-30, "REDEEM"),
        (-10, "EXPIRE"),
        (5, "ADJUST"),
        (-5, "ADJUST"),
    ],
)
def test_lifetime_rule_matches_edge(delta: int, reason: str) -> None:
    """Ràng buộc #6: trung tâm cộng `lifetime_earned` tăng dần giống hệt cách cửa hàng tính lại.

    Chỉ so với cấu hình mặc định (`RETURN_DEMOTE_TIER_ON_RETURN=false`) — cửa hàng không có
    nhánh cho cấu hình kia (docstring `lifetime_earned_of`).
    """
    rules = ReturnRules(_env_file=None)
    edge = lifetime_earned_of([LedgerEntry(delta=delta, reason=reason)])  # type: ignore[arg-type]
    assert lifetime_contribution(delta, reason, rules) == edge


def test_return_demotes_only_when_configured() -> None:
    demote = ReturnRules(_env_file=None, demote_tier_on_return=True)
    assert lifetime_contribution(-20, "RETURN", demote) == -20
    # Tiêu điểm KHÔNG BAO GIỜ làm tụt hạng, kể cả khi cấu hình trả hàng bật.
    assert lifetime_contribution(-30, "REDEEM", demote) == 0
