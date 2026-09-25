"""Profile giả lập — docs/18 §4. Mọi con số là GIẢ ĐỊNH cấu hình được, không phải hằng số.

Có dữ liệu bán hàng thật thì chỉnh file `profiles/*.toml`, không sửa code.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

__all__ = ["PROFILES_DIR", "Profile", "load_profile"]

PROFILES_DIR = Path(__file__).parent / "profiles"

PAYMENT_PLANS = ("CASH", "CARD", "EWALLET", "SPLIT")


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    stores: int
    sales_per_store_day: float
    open_hour: int = 7
    close_hour: int = 22
    #: (giờ bắt đầu, giờ kết thúc — không gồm, tỉ lệ doanh số NGÀY rơi vào khung đó).
    #: Phần còn lại chia đều cho các giờ mở cửa khác. Mặc định: hai đỉnh ở docs/02 §1.
    peak_windows: tuple[tuple[int, int, float], ...] = ((11, 13, 0.25), (17, 20, 0.35))
    weekend_factor: float = 1.3
    #: Số dòng/đơn = 1 + Poisson(lines_extra_mean) → trung bình 3,5 (giả định A1).
    lines_extra_mean: float = 2.5
    #: Zipf trên danh mục sản phẩm: ~20% mã chiếm ~80% lượt bán.
    zipf_s: float = 1.1
    #: Độ nhiễu (σ của log-chuẩn) khi xếp hạng phổ biến theo giá: 0 = đúng thứ tự rẻ → đắt,
    #: càng lớn càng gần xáo ngẫu nhiên. Bán lẻ thật: đồ rẻ bán nhiều hơn hẳn, nhưng không tuyệt
    #: đối (nước mắm bán chạy hơn tăm bông dù đắt hơn).
    price_rank_noise: float = 1.0
    qty_multi_share: float = 0.2
    qty_multi_max: int = 5
    #: Tỉ lệ đơn có khách định danh (giả định A2).
    identified_share: float = 0.6
    #: Tỉ lệ ĐƠN là của khách đăng ký mới ngay tại quầy.
    new_customer_share: float = 0.03
    payment_mix: tuple[tuple[str, float], ...] = (
        ("CASH", 0.70),
        ("CARD", 0.20),
        ("EWALLET", 0.05),
        ("SPLIT", 0.05),
    )
    shifts_per_day: int = 2
    opening_cash: int = 500_000
    #: Phần đầu/cuối mỗi ca không xếp đơn — khoảng giao ca. Đơn đang bay lúc đóng ca sẽ bị
    #: `409 SHIFT_CLOSED`; quầy thật cũng không bán trong lúc đếm két.
    changeover_guard: float = 0.02

    def __post_init__(self) -> None:
        if not 0 <= self.open_hour < self.close_hour <= 24:
            raise ValueError("open_hour/close_hour không hợp lệ")
        if sum(share for *_, share in self.peak_windows) > 1:
            raise ValueError("tổng tỉ lệ các khung cao điểm vượt 1")
        if abs(sum(w for _, w in self.payment_mix) - 1) > 1e-9:
            raise ValueError("payment_mix phải cộng lại bằng 1")
        unknown = {m for m, _ in self.payment_mix} - set(PAYMENT_PLANS)
        if unknown:
            raise ValueError(f"hình thức thanh toán lạ: {sorted(unknown)}")
        if not 0 <= self.new_customer_share <= self.identified_share <= 1:
            raise ValueError("cần 0 ≤ new_customer_share ≤ identified_share ≤ 1")
        if self.shifts_per_day < 1:
            raise ValueError("shifts_per_day ≥ 1")

    @property
    def day_hours(self) -> int:
        return self.close_hour - self.open_hour

    def hour_weights(self) -> list[float]:
        """Tỉ lệ doanh số của từng giờ mở cửa (tổng = 1), theo `peak_windows`."""
        hours = list(range(self.open_hour, self.close_hour))
        weights = dict.fromkeys(hours, 0.0)
        in_peak: set[int] = set()
        for start, end, share in self.peak_windows:
            span = [h for h in range(start, end) if h in weights]
            for h in span:
                weights[h] += share / len(span)
            in_peak.update(span)
        rest = [h for h in hours if h not in in_peak]
        leftover = 1 - sum(weights.values())
        for h in rest:
            weights[h] += leftover / len(rest)
        return [weights[h] for h in hours]


def load_profile(name_or_path: str) -> Profile:
    """`t0`, `t1`... (trong `profiles/`) hoặc đường dẫn tới một file `.toml`."""
    path = Path(name_or_path)
    if not path.suffix:
        path = PROFILES_DIR / f"{name_or_path}.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if "peak_windows" in data:
        data["peak_windows"] = tuple(tuple(w) for w in data["peak_windows"])
    if "payment_mix" in data:
        data["payment_mix"] = tuple(data["payment_mix"].items())
    return Profile(**data)
