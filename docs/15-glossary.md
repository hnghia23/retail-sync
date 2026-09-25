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
| **bronze / silver / gold** | Ba lớp của lake/warehouse: thô bất biến (Parquet trên MinIO + `bronze_*` trong ClickHouse) / sạch, bản mới nhất theo khóa (**staging dbt** trong ClickHouse, không phải Parquet thứ hai) / mô hình chiều (`dim_*`, `fact_*`) | — |
| **FR / NFR** | Functional Requirement / Non-Functional Requirement, đánh số trong [01-requirements](01-requirements.md) | — |
| **AT-xx** | Acceptance Test — kịch bản nghiệm thu nghiệp vụ | CH-xx (chaos), LD-xx (load), DI-xx (data integrity) |
| **ADR** | Architecture Decision Record — quyết định kèm lý do và phương án đã loại | — |
| **giai đoạn A / B / C** | Thứ tự làm theo [ADR-010](adr/010-data-flow-first.md): luồng dữ liệu / chứng minh ổn định & scale / tính năng | *lớp A/B/C* (ba lớp truy vấn — không liên quan) |
| **chặng S1…S6** | Sáu chặng của luồng dữ liệu: ghi cửa hàng · đẩy lên · áp ở trung tâm · trích xuất · nạp ClickHouse · biến đổi dbt ([17 §2](17-data-flow.md)) | — |
| **bộ giả lập** (simulator) | Nguồn dữ liệu của giai đoạn A/B, thay UI. Ba chế độ `edge` / `virtual` / `bulk` ([18](18-simulator.md)) | *seed* (nạp master data một lần) |
| **cửa hàng ảo** | Cửa hàng do bộ giả lập đóng vai ở phía mạng, đẩy thẳng `POST /events`, không có stack edge riêng | Cửa hàng mô phỏng trong compose (có Postgres thật) |
| **manifest** | Đáp án do bộ giả lập ghi: tổng kỳ vọng theo `(store_id, business_date)` và theo khách | — |
| **đối soát xuyên tầng** | So cùng một con số qua L0 manifest → L1 cửa hàng → L2 trung tâm → L3 bronze → L4 marts ([17 §5](17-data-flow.md)) | Đối soát INV-4 (chỉ ledger vs snapshot ở trung tâm) |
| **`CONVERGED` / `CONVERGING` / `DIVERGED`** | Ba kết quả của bộ đối soát: khớp / đang chảy, chưa đủ nhưng không sai / sự cố | — |
| **mốc trích xuất** (`extract_horizon()`) | Mép cuối cửa sổ trích xuất: nhỏ hơn cả `now() - safety_lag` lẫn `xact_start` của transaction đang mở cũ nhất, để dòng commit muộn không lọt ([17 §4](17-data-flow.md) bẫy 1) | `recorded_at` (mốc của từng dòng) |
| **độ tươi** (freshness) | `now() - max(occurred_at)` ở một tầng — dữ liệu ở tầng đó cũ bao lâu | Độ trễ đồng bộ (`recorded_at - occurred_at` của một sự kiện) |
| **lake là trạng thái** | Trích xuất không có bảng watermark: cửa sổ mới bắt đầu ở mép cuối của file cuối cùng trong lake, file đã ghi không bao giờ ghi lại ([17 §2](17-data-flow.md)) | Watermark lưu trong DB |
| **file mốc** | File Parquet rỗng S4 ghi cho bảng incremental đang rỗng, để bảng đó có mặt trong nhật ký nạp | — |
| **mép "đã nạp tới"** (`loaded_until()`) | Mốc mà MỌI bảng incremental đã nạp liền mạch tới (min trên nhật ký nạp). Staging dbt chỉ nhìn phần dưới mốc, để các fact thấy cùng một lát cắt | `extract_horizon()` (mép của trích xuất, ở Postgres) |
| **tập tháng bị ảnh hưởng** | Tháng của `occurred_at` của các dòng mới lần này. Fact dựng lại TRỌN đúng các tháng đó (`insert_overwrite`, bẫy 5) | "tháng hiện tại" (sai với dữ liệu đến trễ) |
| **inferred member** | Dòng dim tạm (`is_inferred = 1`) cho khóa thấy trong giao dịch mà master data chưa có, hoặc ca đang mở chưa lên trung tâm | Dữ liệu bị mất |
| **`net_amount`** | `line_total − discount_allocated` của một dòng fact. Σ theo đơn = `total` = Σ thanh toán | `line_total` (trước chiết khấu) |
| **tật** (quirk) | Hành vi "bẩn" của dữ liệu thật mà bộ giả lập bật có chủ đích: `offline`, `resend`, `concurrent_customer` ([18 §5](18-simulator.md)) | Lỗi của bộ giả lập |
| **kịch bản compose** (`tests/scenarios/`) | Test end-to-end trên compose thật, **opt-in** (`make test-scenarios`) vì đổi trạng thái stack đang chạy | Test tích hợp (testcontainers, tự dựng tự hủy) |

## Vai trò (role)

| Role | Phạm vi | Có thể làm |
|---|---|---|
| `cashier` (thu ngân) | 1 cửa hàng | Bán, trả hàng, mở ca |
| `manager` (quản lý cửa hàng) | 1 cửa hàng | + đóng ca, duyệt chiết khấu vượt ngưỡng, hủy đơn |
| `region_manager` (quản lý khu vực) | Nhiều cửa hàng cùng `region_id` | Đọc báo cáo so sánh (không sửa dữ liệu) |
| `admin` (trụ sở) | Toàn hệ thống | Quản lý master data, xem toàn bộ báo cáo |

---
*Changelog: 2026-09-25 — silver = staging dbt; thêm thuật ngữ hiện thực giai đoạn A (lake là trạng
thái, file mốc, mép "đã nạp tới", tập tháng bị ảnh hưởng, inferred member, `net_amount`, tật,
kịch bản compose).*

*Changelog: 2026-09-23 — thêm thuật ngữ của luồng dữ liệu và bộ giả lập (ADR-010).*

*Changelog: 2026-09-11 — tạo mới.*
