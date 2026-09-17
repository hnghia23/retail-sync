"""Vai trò và quyền tối thiểu cho route — FR-P01, FR-P02, docs/13 §1.

Thuần: không DB, không JWT, không argon2. Đó là việc của `shared.security` (crypto) và
`edge.pos.adapters` (đọc `employee_cache`, giải mã token từ header).

## Xếp hạng vai trò — chỉ áp dụng cho route TẠI CỬA HÀNG

docs/15-glossary.md liệt kê 4 vai trò, nhưng `region_manager` có phạm vi KHÁC LOẠI chứ
không "cao hơn" `manager`: nó đọc báo cáo nhiều cửa hàng, không thao tác một cửa hàng cụ
thể. Mọi route trong bảng docs/13 §1 (module `pos`) chỉ yêu cầu tối thiểu `cashier` hoặc
`manager` — nên thứ tự tuyến tính dưới đây là ĐỦ cho *route edge*, dù không phản ánh đúng
quan hệ giữa `region_manager` và `manager` trong toàn hệ thống. Không dùng bảng này để suy
luận quyền ở Central API.
"""

from __future__ import annotations

from shared.types import Role

_RANK: dict[Role, int] = {
    "cashier": 0,
    "manager": 1,
    "region_manager": 2,
    "admin": 3,
}


class UnknownRoleError(ValueError):
    """Trung tâm thêm vai trò mới, cửa hàng chưa cập nhật danh sách xếp hạng."""


def meets_minimum(role: Role, minimum: Role) -> bool:
    """`True` nếu `role` đủ quyền cho một route yêu cầu tối thiểu `minimum`.

    Vai trò lạ (không có trong `_RANK`) → từ chối, KHÔNG mặc định cho qua. Khác với hạng
    khách (`tier_discount_pct` trả 0% cho hạng lạ, docs/05 §5) — sai lệch ở đây là lỗ hổng
    phân quyền, không phải một khoản giảm giá bị thiếu.
    """
    try:
        return _RANK[role] >= _RANK[minimum]
    except KeyError as exc:
        raise UnknownRoleError(f"Vai trò không rõ: {exc}") from exc
