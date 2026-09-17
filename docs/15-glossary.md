# 15 — Glossary (thuật ngữ dùng thống nhất)

> Dùng đúng từ này trong code, docs, và trao đổi — tránh mỗi chỗ gọi một tên cho cùng một
> khái niệm (VD: đừng lẫn `sale` với `order`, `transaction`).

| Thuật ngữ | Định nghĩa | Không nhầm với |
|---|---|---|
| **sale** | Một giao dịch bán/trả hàng tại quầy | *order* (không dùng), *transaction* (chỉ dùng cho DB transaction) |
| **shift** | Một ca làm việc của một hoặc nhiều thu ngân, từ mở tới đóng | *session* (đăng nhập), *business_date* |
| **business_date** | Ngày kinh doanh — ca kéo qua nửa đêm vẫn tính vào 1 ngày | *ngày lịch* của `occurred_at` |
| **occurred_at** | Thời điểm sự kiện xảy ra **tại cửa hàng** | `recorded_at` |
| **recorded_at** | Thời điểm **trung tâm nhận được** sự kiện | `occurred_at` — có thể lệch hàng giờ/ngày |
| **event_id** | UUIDv7 sinh tại cửa hàng, khóa idempotency toàn hệ thống | `sale_id` (khác mục đích dù cùng dạng UUID) |
| **point_ledger** | Sổ cái điểm — chỉ ghi thêm, không update | `point_balance` (snapshot suy ra từ ledger) |
| **lifetime_earned** | Tổng điểm **từng tích được**, dùng để xếp hạng | `balance` (số dư hiện tại, dùng để redeem) |
| **tier** | Hạng thành viên (Bronze/Silver/Gold/Platinum) | — |
| **outbox** | Bảng trung gian ghi sự kiện chờ đồng bộ, cùng transaction với dữ liệu nghiệp vụ | Message queue (không phải Kafka/Redpanda ở v1) |
| **master data** | Dữ liệu trung tâm sở hữu: sản phẩm, giá, khuyến mãi, quy tắc hạng, nhân viên | Dữ liệu giao dịch (cửa hàng sở hữu) |
| **cửa hàng tự chủ** (store autonomy) | Nguyên tắc: bán được hàng khi mất mạng hoàn toàn | — |
| **ranh giới chịu lỗi** | Điểm nối có thể đứt bất cứ lúc nào (cửa hàng ↔ trung tâm) | Ranh giới module (nội bộ, không nên đứt) |
| **lớp A / B / C** | Ba lớp truy vấn: vận hành (T+0, PG cửa hàng) / quản lý (T+0-T+1, PG trung tâm) / phân tích (T+1, warehouse) | — |
| **scale seam** | Điểm nâng cấp đã thiết kế sẵn khi chạm trần quy mô (VD: thêm PgBouncer) | Refactor tùy hứng |
| **bronze / silver / gold** | Ba lớp của lake/warehouse: thô bất biến / sạch khử trùng / mô hình chiều | — |
| **FR / NFR** | Functional Requirement / Non-Functional Requirement, đánh số trong [01-requirements](01-requirements.md) | — |
| **AT-xx** | Acceptance Test — kịch bản nghiệm thu nghiệp vụ | CH-xx (chaos), LD-xx (load), DI-xx (data integrity) |
| **ADR** | Architecture Decision Record — quyết định kèm lý do và phương án đã loại | — |

## Vai trò (role)

| Role | Phạm vi | Có thể làm |
|---|---|---|
| `cashier` (thu ngân) | 1 cửa hàng | Bán, trả hàng, mở ca |
| `manager` (quản lý cửa hàng) | 1 cửa hàng | + đóng ca, duyệt chiết khấu vượt ngưỡng, hủy đơn |
| `region_manager` (quản lý khu vực) | Nhiều cửa hàng cùng `region_id` | Đọc báo cáo so sánh (không sửa dữ liệu) |
| `admin` (trụ sở) | Toàn hệ thống | Quản lý master data, xem toàn bộ báo cáo |

---
*Changelog: 2026-09-11 — tạo mới.*
