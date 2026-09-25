# ADR-010 — Luồng dữ liệu trước, tính năng sau

- **Trạng thái:** ✅ Chấp nhận (2026-09-23)
- **Người quyết định:** chủ dự án
- **Liên quan:** [08-reliability-and-scale](../08-reliability-and-scale.md) (ưu tiên ổn định),
  [06-roadmap](../06-roadmap.md) (lộ trình được viết lại theo ADR này),
  [17-data-flow](../17-data-flow.md), [18-simulator](../18-simulator.md)
- **Không thay thế ADR nào.** Stack giữ nguyên; ADR này đổi **thứ tự làm**, không đổi công nghệ.

---

## Bối cảnh

Lộ trình cũ ([06](../06-roadmap.md), bản 2026-09-11) xếp mỗi tuần theo **tính năng của ứng
dụng**: tuần 1 POS + UI, tuần 2 loyalty + đồng bộ + trả hàng + chốt ca + báo cáo, tuần 3
warehouse, tuần 4 mới chứng minh ổn định. Tới giữa tuần 2 (2026-09-23), tình hình là:

1. **Đường ghi của cửa hàng đã chạy thật** (`PlaceSale` → sale + ledger + outbox trong một
   transaction), và **đồng bộ lên trung tâm đã chạy thật**, kiểm chứng trên compose.
2. Việc còn lại của tuần 2 phần lớn là **tính năng dùng tại quầy**: tra khách 3 bước, đăng
   ký khách qua UI, trả hàng, chốt ca, báo cáo ca. Mỗi thứ đều tốn thời gian, và **không cái
   nào giúp chứng minh hệ thống ổn định**.
3. Nửa sau của luồng dữ liệu (trung tâm → lake → warehouse) **chưa có dòng code nào**, còn
   bằng chứng ổn định (hỗn loạn, ngâm, tải) bị đẩy về tuần cuối — đúng chỗ dễ bị cắt nhất
   khi hết thời gian.

Ưu tiên số 1 của dự án là *"hệ thống chạy ổn định, khả năng scale tốt — đứng trên độ đầy đủ
tính năng"*. Lộ trình cũ nói điều đó nhưng **làm ngược lại**: tính năng xếp trước, còn bằng
chứng bị dồn về cuối.

Chủ dự án chốt (2026-09-23): *"chỉ cần tập trung vào phát triển luồng dữ liệu, không cần
dựng cả ứng dụng để sử dụng, chỉ cần tạo dữ liệu giả lập để test hiệu năng sau khi đã tạo
được luồng dữ liệu để chứng minh tính ổn định, sau đó mới phát triển các tính năng khác."*

## Quyết định

Làm theo **ba giai đoạn tuần tự**. Chưa qua cổng của giai đoạn trước thì không sang giai đoạn sau.

| Giai đoạn | Mục tiêu | Xong khi |
|---|---|---|
| **A — Luồng dữ liệu thông suốt** | Dữ liệu đi hết đường: cửa hàng → outbox → trung tâm → bronze → ClickHouse, **đúng và đủ** ở mọi tầng | Cổng A ([06](../06-roadmap.md)): đối soát xuyên tầng khớp tuyệt đối, chạy lại không nhân đôi |
| **B — Chứng minh ổn định & scale** | Bộ giả lập chạy tải thật lên luồng đó, cộng thêm hỏng hóc có chủ đích | Cổng B = nghiệm thu POC: CH-1…7, test ngâm 72h, LD-1…4, scale seam đo bằng số |
| **C — Tính năng** | Những gì người dùng tại quầy và trụ sở cần | Theo backlog, **chỉ bắt đầu sau cổng B** |

### Quy tắc đi kèm

1. **Bộ giả lập thay chỗ UI làm nguồn dữ liệu.** Trong A và B không làm thêm màn hình nào.
   UI POS đã có của tuần 1 **được giữ nguyên và đóng băng**: vẫn chạy, vẫn có test, nhưng không
   mở rộng.
2. **Trong A/B chỉ viết code tính năng khi thiếu nó thì luồng dữ liệu thiếu một loại dữ liệu.**
   Ví dụ: `RegisterCustomer` là **bắt buộc**, vì không có nó thì không có `CustomerCreated`
   và không có điểm thật nào chảy qua đồng bộ. Ngược lại, tra khách 3 bước **không** bắt buộc,
   vì nó không sinh ra dữ liệu mới nào.
   Code thêm theo quy tắc này là **use case + route JSON**, không kèm UI.
3. **Bộ giả lập đi qua đúng đường ghi production.** Ở chế độ đầu-cuối, nó gọi Edge API thật
   (`POST /sales`, `POST /customers`...), cho các use case thật chạy, trong transaction thật.
   **Không được `INSERT` thẳng vào bảng giao dịch của cửa hàng.** Nếu làm vậy thì phép chứng
   minh chỉ chứng minh được bộ giả lập, không chứng minh được code.
4. **Dữ liệu nạp khối (bulk) chỉ dùng để đo khối lượng, không dùng để chứng minh tính đúng.**
   256 triệu dòng ở T2 không thể đi qua HTTP trong thời gian của dự án
   ([18 §2](../18-simulator.md)), nên nạp thẳng vào bronze. Nhưng tính đúng của luồng chỉ
   được chứng minh bằng dữ liệu đã **thật sự đi qua luồng**.
5. **Bộ giả lập không được giả thời gian qua API.** `occurred_at` do cửa hàng tự đóng dấu.
   Giả lập nhiều ngày thì dùng chế độ cửa hàng ảo hoặc bulk ([18 §3](../18-simulator.md)),
   không mở cửa hậu cho client tự khai thời điểm.

### Phân loại lại công việc

| Hạng mục | Trước (lộ trình cũ) | Sau |
|---|---|---|
| `RegisterCustomer` + `POST /customers` (không UI) | Tuần 2, kèm UI | **A — Must** |
| Seed nhân viên cho bộ giả lập đăng nhập | Việc nhỏ đầu tuần 2 | **A — Must** |
| `OpenShift` thật thay route tạm `/ui/shifts/open` | Tuần 2 ngày 10 | **A — Must** (mọi `sale` cần `shift_id`) |
| `CloseShift` + `ShiftClosed` | Tuần 2, kèm báo cáo ca | **A — Must** ⬆️ (ban đầu xếp Should, nâng lên 2026-09-23 — xem changelog) |
| Trả hàng (`ReturnItems`, mở rộng payload `SaleReturned`) | Tuần 2 | **A — Should** (dữ liệu âm cho ledger/fact) |
| Job đối soát INV-4 tăng dần | Tuần 2 ngày 10 | **A — Must** |
| Trích xuất → bronze → ClickHouse → dbt → Airflow | Tuần 3 | **A — Must** |
| Bộ giả lập (3 chế độ) | Tuần 4 ngày 16 | **A — Must** (kéo lên sớm nhất có thể) |
| Hỗn loạn, ngâm, tải, scale seam | Tuần 4 | **B — Must** |
| Tra khách 3 bước + làm mới cache | Tuần 2 ngày 9 | **C** |
| Báo cáo ca/ngày/thu ngân (FR-R01…R03) | Tuần 2 ngày 10 — Must | **C** |
| Kéo master data xuống cửa hàng (FR-C01) | Tuần 2 ngày 10 | **C** (cửa hàng dùng `product_cache` đã seed) |
| UI đăng ký khách, UI trả hàng, UI chốt ca | Tuần 2 | **C** |
| `GET /sync/status` cho trụ sở | Tuần 2 | **C** (dữ liệu đã có ở `store_sync_status` và metric) |
| Metabase + dashboard nghiệp vụ | Tuần 4 — Could | **C** |
| Công cụ test tải | `k6` (08 §6.3) | **Bộ giả lập** — một bộ sinh dữ liệu, một manifest ([18 §9](../18-simulator.md)) |
| Silver dạng Parquet trên MinIO | Tuần 3 ngày 12 (cắt đầu tiên nếu trễ) | **v2** — silver = staging dbt trong ClickHouse ([17 §2](../17-data-flow.md)) |

## Hệ quả

### Tích cực
- **Bằng chứng ổn định có trước tính năng.** Tính năng nào thêm sau cũng chạy trên một luồng
  đã được chứng minh, và bộ test hỗn loạn/tải từ B trở thành lưới an toàn cho C.
- **Phát hiện lỗi thiết kế ở hạ nguồn sớm hơn.** Mọi lỗi của đồng bộ mà tuần 2 tìm được
  (docs/progress 2026-09-23 §2) đều chỉ lộ ra khi dữ liệu thật sự chảy. Nửa sau của luồng
  (trích xuất theo watermark, dữ liệu đến trễ, idempotency ClickHouse) chắc chắn cũng có lỗi
  loại đó, và càng biết sớm càng rẻ. [17 §4](../17-data-flow.md) đã liệt kê năm bẫy như vậy
  chỉ bằng cách đọc lại schema.
- **Bộ giả lập biết "đáp án đúng".** Vì chính nó sinh ra dữ liệu, nó ghi được tổng kỳ vọng
  (số đơn, doanh thu, điểm của từng khách), và mọi tầng phải khớp tổng đó. Dữ liệu tay qua UI
  không làm được việc này.

### Tiêu cực — chấp nhận có chủ đích
- **Không demo được cho người dùng cuối** cho tới giai đoạn C. Không có luồng tại quầy hoàn
  chỉnh: không tra được khách lạ, không trả hàng qua UI, không in báo cáo ca.
- **Một số kịch bản nghiệm thu nghiệp vụ lùi về C:** AT-05 (cửa hàng mới lấy số dư từ trung
  tâm) và AT-09 (lên hạng áp dụng đơn sau). AT-06 (trả hàng) chỉ vào A nếu kịp phần Should.
- **Lớp truy vấn A (báo cáo T+0 tại cửa hàng)** tạm chưa có, dù B03 đánh giá đây là lớp dùng
  nhiều nhất. Chấp nhận được vì chỉ là truy vấn đọc trên dữ liệu **đã có sẵn** ở Postgres
  cửa hàng: không đổi schema, không đổi luồng, thêm sau không phải sửa gì phía trước.

### Rủi ro
| Rủi ro | Giảm thiểu |
|---|---|
| Bộ giả lập sinh dữ liệu "quá sạch", không lộ lỗi mà dữ liệu thật sẽ lộ | Bộ giả lập có sẵn các "tật" của dữ liệu thật: đến trễ, trùng SĐT (C03), khách mua nhiều cửa hàng, cửa hàng offline dài ([18 §5](../18-simulator.md)) |
| Làm C xong mới thấy luồng A thiếu dữ liệu cho tính năng | Hợp đồng sự kiện (docs/12) và schema (docs/05) **đã đủ** cho mọi tính năng C. C chỉ thêm route đọc và UI, không thêm cột bắt buộc nào |
| Hết thời gian giữa chừng B | Cắt theo MoSCoW ở [06](../06-roadmap.md), từ dưới lên. **Không** chuyển sang C để "có cái demo" |

## Điều kiện xem lại

- Có người dùng thật (thu ngân, cửa hàng pilot) cần dùng **trước** khi B xong → kéo đúng tính
  năng đó lên, ghi changelog ở doc này.
- Cổng B trượt vì lý do kiến trúc (không phải vì thiếu thời gian) → dừng C, xem lại ADR-001/003.

## Tiến độ theo quyết định này

- **Giai đoạn A xong, 🚪 cổng A đạt 2026-09-24** — cả 8 điều kiện kiểm trên compose thật
  ([06 §Cổng A](../06-roadmap.md), [progress/2026-09-24-cong-a.md](../progress/2026-09-24-cong-a.md)).
  Mọi việc A — Must ở bảng phân loại trên đã làm, trừ dashboard "sức khỏe luồng" (dời lên đầu B).
- **A — Should "Trả hàng" đã cắt**, đúng phương án "nếu trượt" của cổng A.
- Lợi ích dự đoán ở §Hệ quả ("phát hiện lỗi thiết kế ở hạ nguồn sớm hơn") xảy ra thật: bộ giả lập
  và DAG chạy trong giờ bán lộ ra 10+ lỗi mà chế độ dùng UI sẽ không lộ, tiêu biểu là partition
  `point_ledger` tháng trước ở tháng go-live, ca đang mở chưa có ở trung tâm, và bảng rỗng không
  có file mốc ([progress/](../progress/)).
- Tiếp theo: **giai đoạn B**.

---
*Changelog: 2026-09-25 — thêm mục "Tiến độ theo quyết định này" (cổng A đạt).*

*Changelog: 2026-09-23 (lần 2) — `CloseShift` từ Should lên **Must**. Mỗi cửa hàng chỉ được một ca
mở, và `business_date` của đơn lấy từ ca, nên không đóng được ca thì mọi đơn kẹt ở ngày mở
ca đầu tiên, và đối soát theo `(store_id, business_date)` vô nghĩa. Trên compose, ca mở từ
17/09 đúng là chưa đóng suốt 6 ngày, nên mọi đơn tới 23/09 đều mang `business_date = 17/09`.*

*Changelog: 2026-09-23 — tạo mới theo quyết định của chủ dự án.*
