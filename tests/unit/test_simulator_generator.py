"""Bộ sinh ý định — thuần, không cần Docker (docs/18 §8, §10 bước 1)."""

from __future__ import annotations

import statistics
from datetime import date
from typing import Any

import pytest

from shared.pii import normalize_vn_phone
from simulator.generator import CloseShift, Intent, OpenShift, Sale, plan_store
from simulator.profile import Profile, load_profile

CATALOG = [f"SKU-{i:04d}" for i in range(500)]
MONDAY = date(2026, 9, 21)


def _plan(profile: Profile | None = None, **kw: Any) -> list[Intent]:
    args: dict[str, Any] = {
        "store_id": "store-001",
        "catalog": CATALOG,
        "seed": 42,
        "nonce": 7,
        "days": 5,
        "rate": 60.0,
        "start_date": MONDAY,
    } | kw
    return plan_store(profile or load_profile("t0"), **args)


def _sales(intents: list[Intent]) -> list[Sale]:
    return [i for i in intents if isinstance(i, Sale)]


def test_profiles_load_and_hour_weights_sum_to_one() -> None:
    for name in ("t0", "t1"):
        p = load_profile(name)
        assert abs(sum(p.hour_weights()) - 1) < 1e-9
        assert len(p.hour_weights()) == p.day_hours


def test_same_seed_same_plan_different_seed_different_plan() -> None:
    """Tái lập (docs/18 §1): một lần chạy lỗi phải chạy lại y hệt để gỡ lỗi."""
    assert _plan() == _plan()
    assert _plan() != _plan(seed=43)


def test_other_stores_do_not_shift_a_stores_plan() -> None:
    """Mỗi cửa hàng một luồng ngẫu nhiên: thêm cửa hàng không đổi lịch cửa hàng cũ."""
    assert _plan(store_id="store-001") != _plan(store_id="store-002")
    assert _plan(store_id="store-001") == _plan(store_id="store-001")


def test_every_sale_lies_inside_an_open_shift_away_from_changeover() -> None:
    """Không đơn nào rơi vào lúc giao ca — đơn đó sẽ bị `409 SHIFT_CLOSED` ở cửa hàng."""
    intents = _plan()
    open_at: float | None = None
    for it in intents:
        if isinstance(it, OpenShift):
            assert open_at is None, "mở ca khi ca trước chưa đóng"
            open_at = it.at
        elif isinstance(it, CloseShift):
            assert open_at is not None
            open_at = None
        else:
            assert open_at is not None, f"đơn {it.intent_id} nằm ngoài ca"
    assert open_at is None


def test_distribution_matches_profile_assumptions() -> None:
    """A1 (3,5 dòng/đơn), A2 (60% có khách), cơ cấu thanh toán, hai đỉnh trưa/tối."""
    profile = load_profile("t0")
    sales = _sales(_plan(days=30))
    n = len(sales)
    assert 30 * 100 * 0.9 < n < 30 * 100 * 1.3 * 1.1  # có cuối tuần ×1,3

    assert statistics.mean(len(s.lines) for s in sales) == pytest.approx(3.5, abs=0.3)
    assert sum(s.customer is not None for s in sales) / n == pytest.approx(0.6, abs=0.03)
    assert sum(s.payment == "CASH" for s in sales) / n == pytest.approx(0.70, abs=0.03)

    day_real = profile.day_hours * 3600 / 60
    hours = [int(((s.at % day_real) * 60) // 3600) + profile.open_hour for s in sales]
    lunch = sum(11 <= h < 13 for h in hours) / n
    evening = sum(17 <= h < 20 for h in hours) / n
    assert lunch == pytest.approx(0.25, abs=0.04)
    assert evening == pytest.approx(0.35, abs=0.04)


def test_product_popularity_is_skewed() -> None:
    """Zipf: nhóm 20% mã bán chạy nhất chiếm phần lớn lượt bán."""
    counts: dict[str, int] = {}
    for s in _sales(_plan(days=30)):
        for product, _ in s.lines:
            counts[product] = counts.get(product, 0) + 1
    ranked = sorted(counts.values(), reverse=True)
    top = sum(ranked[: len(CATALOG) // 5])
    assert top / sum(ranked) > 0.6


def test_customers_are_registered_before_they_are_reused() -> None:
    """Khách quen chỉ xuất hiện SAU đơn đăng ký của chính họ — sink đợi đúng thứ tự này."""
    seen: set[str] = set()
    new_count = 0
    for s in _sales(_plan(days=10)):
        if s.customer is None:
            continue
        if s.customer.new:
            assert s.customer.ref not in seen
            seen.add(s.customer.ref)
            new_count += 1
        else:
            assert s.customer.ref in seen
    assert new_count > 0 and len(seen) == new_count


def test_phones_are_valid_unique_and_depend_on_nonce() -> None:
    """Đúng dạng cửa hàng chấp nhận; không trùng trong một lần chạy; khác nonce → khác số,
    để chạy lại trên cùng DB không vô tình dùng lại khách của lần trước."""
    phones = [s.customer.phone for s in _sales(_plan(days=10)) if s.customer and s.customer.new]
    assert len(phones) == len(set(phones))
    assert all(normalize_vn_phone(p) == p for p in phones)

    other = [
        s.customer.phone for s in _sales(_plan(days=10, nonce=8)) if s.customer and s.customer.new
    ]
    assert set(phones).isdisjoint(other)


def test_rate_compresses_time_linearly() -> None:
    slow = _plan(rate=60.0, days=1)
    fast = _plan(rate=600.0, days=1)
    assert [type(i) for i in slow] == [type(i) for i in fast]
    assert fast[-1].at == pytest.approx(slow[-1].at / 10)


def test_invalid_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="payment_mix"):
        Profile(name="x", stores=1, sales_per_store_day=1, payment_mix=(("CASH", 0.5),))
    with pytest.raises(ValueError, match="identified_share"):
        Profile(name="x", stores=1, sales_per_store_day=1, new_customer_share=0.9)


def test_price_bias_makes_cheap_products_sell_more() -> None:
    """Có `prices` → đồ rẻ đứng đầu bảng phổ biến; giá trung bình mỗi lượt bán giảm rõ.

    Không có thiên lệch này, lần chạy thật đầu tiên ra 3,1 triệu/đơn với danh mục có giá trung
    vị 220k — một mã 49 triệu có thể thành "hàng bán chạy nhất".
    """
    prices = {sku: 10_000 * (i + 1) for i, sku in enumerate(CATALOG)}  # 10k → 5 triệu

    def mean_price(plan: list[Intent]) -> float:
        picks = [prices[p] for s in _sales(plan) for p, _ in s.lines]
        return statistics.mean(picks)

    unbiased = mean_price(_plan(days=10))
    biased = mean_price(_plan(days=10, prices=prices))
    assert biased < unbiased * 0.5
    assert _plan(prices=prices) == _plan(prices=prices)  # vẫn tái lập theo seed
