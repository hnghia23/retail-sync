"""Manifest — ĐÁP ÁN của một lần chạy (docs/18 §6).

Ở chế độ `edge`, đáp án lấy từ những gì cửa hàng ĐÃ CAM KẾT: response `201` của `POST /sales`
(tổng tiền, điểm) và `200` của đóng ca. Bộ giả lập không tự tính giá. Một đơn đã nhận `201`
thì phải có mặt ở mọi tầng; thiếu ở đâu thì lỗi nằm ở chặng trước tầng đó.

Request mất kết nối hay quá hạn giữa chừng thì không biết đơn đã commit hay chưa → xếp vào
`unknown`. Bộ đối soát chỉ cho phép đơn loại này **có đủ ở mọi tầng hoặc vắng ở mọi tầng**.

Ở chế độ `virtual`, đáp án là chính các envelope cửa hàng ảo đã dựng (docs/18 §6). Cửa hàng ảo
không có Postgres, nên trạng thái outbox cuối của nó ghi vào `StoreRecord.outbox` — bộ đối soát
dùng nó thay cho outbox thật khi quyết "chưa tới" hay "mất".
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Manifest", "SaleRecord", "ShiftRecord", "StoreRecord", "percentiles"]


@dataclass(slots=True)
class SaleRecord:
    sale_id: str
    shift_id: str
    business_date: str
    total: int
    payments: dict[str, int]
    points: int
    customer_id: str | None


@dataclass(slots=True)
class ShiftRecord:
    shift_id: str
    business_date: str
    opening_cash: int
    #: Tiền mặt bộ giả lập đã trả qua các đơn `201` của ca — thứ nó "đếm được" khi đóng ca.
    cash_from_sales: int = 0
    closed: bool = False
    counted_cash: int | None = None
    expected_cash: int | None = None
    variance: int | None = None


@dataclass(slots=True)
class StoreRecord:
    shifts: dict[str, ShiftRecord] = field(default_factory=dict)
    sales: dict[str, SaleRecord] = field(default_factory=dict)
    #: customer_id → {"created": bool}. Chỉ khách `created=true` mới có điểm kỳ vọng tuyệt đối.
    customers: dict[str, dict[str, bool]] = field(default_factory=dict)
    unknown: list[dict[str, str]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    #: Ca đang mở từ trước khi chạy (không thuộc lần chạy này) mà bộ giả lập phải đóng.
    foreign_shifts: list[str] = field(default_factory=list)
    #: Chỉ chế độ `virtual`: outbox trong bộ nhớ của cửa hàng ảo lúc kết thúc
    #: (`pending`, `dead`) — thay cho bảng `outbox` mà cửa hàng thật tự báo.
    outbox: dict[str, int] = field(default_factory=dict)


def percentiles(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    ordered = sorted(samples)
    if len(ordered) == 1:
        v = round(ordered[0], 2)
        return {"n": 1, "p50": v, "p95": v, "p99": v, "max": v}
    q = statistics.quantiles(ordered, n=100, method="inclusive")
    return {
        "n": len(ordered),
        "p50": round(q[49], 2),
        "p95": round(q[94], 2),
        "p99": round(q[98], 2),
        "max": round(ordered[-1], 2),
    }


@dataclass(slots=True)
class Manifest:
    run_id: str
    mode: str
    profile: str
    seed: int
    nonce: int
    days: int
    rate: float
    started_at: str
    finished_at: str | None = None
    stores: dict[str, StoreRecord] = field(default_factory=dict)
    latency_ms: dict[str, list[float]] = field(default_factory=dict)
    scheduler_lag_s: list[float] = field(default_factory=list)
    cpu_share: float | None = None
    #: Cấu hình riêng của lần chạy (chế độ `virtual`: tật, cửa hàng offline, ngày bắt đầu...).
    options: dict[str, Any] = field(default_factory=dict)

    def store(self, store_id: str) -> StoreRecord:
        return self.stores.setdefault(store_id, StoreRecord())

    def observe(self, endpoint: str, elapsed_ms: float) -> None:
        self.latency_ms.setdefault(endpoint, []).append(elapsed_ms)

    def expected_points(self, store_id: str) -> dict[str, int]:
        """Điểm kỳ vọng của từng khách MỚI đăng ký ở `store_id` trong lần chạy.

        Cộng điểm của khách ở MỌI cửa hàng: khách mua cả ở cửa hàng khác (tật
        `concurrent_customer`, AT-04) có một số dư duy nhất ở trung tâm."""
        record = self.stores[store_id]
        points = {cid: 0 for cid, info in record.customers.items() if info.get("created")}
        for other in self.stores.values():
            for sale in other.sales.values():
                if sale.customer_id in points:
                    points[sale.customer_id] += sale.points
        return points

    def summary(self) -> dict[str, Any]:
        """Tổng theo `(store_id, business_date)` — hàng L0 của bảng đối soát ở docs/17 §5."""
        out: dict[str, Any] = {}
        for store_id, record in self.stores.items():
            days: dict[str, dict[str, int]] = {}
            for sale in record.sales.values():
                d = days.setdefault(
                    sale.business_date,
                    {"sales": 0, "total": 0, "CASH": 0, "CARD": 0, "EWALLET": 0, "points": 0},
                )
                d["sales"] += 1
                d["total"] += sale.total
                d["points"] += sale.points
                for method, amount in sale.payments.items():
                    d[method] += amount
            out[store_id] = {
                "by_business_date": days,
                "shifts": len(record.shifts),
                "customers_created": sum(1 for c in record.customers.values() if c["created"]),
                "unknown": len(record.unknown),
                "rejected": len(record.rejected),
            }
        return out

    def stats(self) -> dict[str, Any]:
        return {
            "latency_ms": {k: percentiles(v) for k, v in sorted(self.latency_ms.items())},
            "scheduler_lag_s": percentiles(self.scheduler_lag_s),
            "cpu_share": self.cpu_share,
        }

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("latency_ms")
        data.pop("scheduler_lag_s")
        data["summary"] = self.summary()
        data["stats"] = self.stats()
        return data

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "manifest.json"
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: Path) -> Manifest:
        data = json.loads(path.read_text(encoding="utf-8"))
        stores = {
            sid: StoreRecord(
                shifts={k: ShiftRecord(**v) for k, v in rec["shifts"].items()},
                sales={k: SaleRecord(**v) for k, v in rec["sales"].items()},
                customers=rec["customers"],
                unknown=rec["unknown"],
                rejected=rec["rejected"],
                foreign_shifts=rec["foreign_shifts"],
                outbox=rec.get("outbox", {}),
            )
            for sid, rec in data["stores"].items()
        }
        return cls(
            run_id=data["run_id"],
            mode=data["mode"],
            profile=data["profile"],
            seed=data["seed"],
            nonce=data["nonce"],
            days=data["days"],
            rate=data["rate"],
            started_at=data["started_at"],
            finished_at=data["finished_at"],
            stores=stores,
            cpu_share=data.get("cpu_share"),
            options=data.get("options", {}),
        )
