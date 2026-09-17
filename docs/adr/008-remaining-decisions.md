# ADR-008 — Chốt các quyết định còn lại

**Trạng thái:** Được chấp nhận · 2026-09-11
**Bối cảnh:** chủ dự án yêu cầu chốt toàn bộ kịch bản trước, kiểm chứng thực nghiệm để sau.

Gộp năm quyết định còn treo (C0–C5) cùng hai quyết định nhỏ (Q2, Q4) vào một ADR, vì mỗi
cái đều nhỏ và chúng liên quan nhau.

---

## C0 — DB tại cửa hàng: **PostgreSQL 18** ✅

Xác nhận lại [ADR-001](001-postgres-everywhere.md) sau phản biện của chủ dự án về chi phí
vận hành.

**Lý do quyết định — theo thứ tự sức nặng:**

1. **Số lượng engine, không phải lựa chọn engine.** DB trung tâm bắt buộc là Postgres. Chọn
   MySQL ở cửa hàng = vận hành hai engine: hai quy trình backup, hai bộ giám sát, hai đường
   nâng cấp, hai phương ngữ SQL, hai bộ driver/testcontainers, hai bộ cạm bẫy.
2. **v1 đã chứng minh chi phí này là thật** — chạy MySQL *và* Postgres ở cửa hàng, rồi
   schema trôi khỏi code ở **cả hai** (lỗi B2, B5, B7 trong [09-v1-postmortem](../09-v1-postmortem.md)).
3. **Điểm yếu của Postgres tỷ lệ thuận với quy mô, mà DB cửa hàng rất nhỏ.** Vài trăm MB,
   một app server, pool ~10 kết nối, không replication. Chênh lệch RAM 30–40% trên ngân sách
   256 MB là ~80 MB — không đáng kể trên mini PC.
4. **Dùng chung migration, model, test fixture với trung tâm.** Với solo dev, viết một lần
   dùng hai nơi là tiết kiệm thật.

**Đã cân nhắc nghiêm túc và loại:**

- **MySQL 8** — phản biện của chủ dự án về vận hành là **đúng** (RAM +30–40%,
  process-per-connection, autovacuum là mối lo lớn nhất của Postgres). Nhưng nó chỉ *nhẹ
  hơn*, không *nhẹ đi*, trong khi kéo theo engine thứ hai.
- **SQLite** — vận hành **bằng 0**, và giới hạn một-writer không phải vấn đề ở tải này
  (~1 đơn/30 giây lúc cao điểm). Loại vì: mất luôn lợi ích "một phương ngữ" (lập luận chính
  của cả quyết định), chặn đường chạy nhiều instance app trong một cửa hàng, và khó quan
  sát/can thiệp từ xa khi vận hành thật.

### ⚠️ Việc bắt buộc kèm theo

Phản biện về autovacuum là đúng và phải xử lý, **không được bỏ qua**:

```sql
-- outbox là bảng DUY NHẤT có mẫu sinh bloat: insert -> update -> delete
ALTER TABLE outbox SET (
    autovacuum_vacuum_scale_factor = 0.02,   -- mặc định 0.2, quá thưa cho bảng này
    autovacuum_vacuum_threshold    = 50,
    autovacuum_analyze_scale_factor = 0.05
);
```

Đưa vào **migration đầu tiên**, không để sau. Kèm job dọn dòng `sent_at` cũ hơn 7 ngày.

---

## C1 — UI POS: **HTMX + Alpine.js**, không build step ✅

**Lý do:**

1. **Tránh toolchain thứ hai.** React kéo theo Node, bundler, dependency tree riêng, và một
   vòng lặp build. Với 1 người / 1 tháng, đó là chi phí thật mà không đổi lại được gì cho
   một giao diện quầy thu ngân.
2. **Logic nghiệp vụ ở lại một chỗ.** Server-rendered nghĩa là tính tiền, tính điểm, áp
   chiết khấu đều chỉ tồn tại trong Python — test được, không có bản sao logic ở client
   để lệch nhau.
3. **Không phải version hóa API.** Không có ranh giới frontend/backend để đồng bộ.
4. UI POS bản chất là luồng form: quét → thêm dòng → sửa số lượng → thanh toán. Đúng vùng
   mạnh của HTMX.

**Vì sao thêm Alpine.js:** giỏ hàng cần trạng thái cục bộ và cập nhật tổng tiền tức thì khi
gõ số lượng — vòng round-trip server cho mỗi lần gõ là trải nghiệm tệ ở quầy. Alpine (~15 KB,
nhúng bằng thẻ `<script>`, không build) xử lý đúng phần đó. Chốt đơn vẫn đi qua server.

**Đã loại:** React/Vue SPA (toolchain thứ hai) · HTMX thuần (giỏ hàng giật) · Streamlit/
Gradio (không phù hợp giao diện giao dịch).

---

## C3 — Tầng biến đổi: **dbt-core** ✅ *(quyết định có căng thẳng, đọc kỹ)*

**Căng thẳng cần thừa nhận:** chủ dự án đặt nguyên tắc *"ưu tiên độ phù hợp thực tế, không
chạy theo độ phổ biến"*. Xét thuần độ phù hợp kỹ thuật, **SQLMesh khớp hơn** — nó hiểu native
"idempotent incremental" và **xử lý dữ liệu đến trễ đúng theo mặc định**, trong khi incremental
của dbt về bản chất là template truy vấn. Mà dữ liệu đến trễ **đúng là đặc tính cốt lõi** của
hệ thống này: cửa hàng offline ba ngày rồi mới đồng bộ.

**Vì sao vẫn chọn dbt — và vì sao đây không phải "chạy theo độ phổ biến":**

Vấn đề dữ liệu đến trễ **giải được bằng thiết kế phân vùng**, không cần đổi công cụ:

> **Quy tắc bắt buộc: phân vùng bronze theo NGÀY NẠP (`recorded_at`), không theo ngày phát
> sinh (`occurred_at`).**
>
> Dữ liệu đến trễ rơi vào phân vùng *hôm nay*, **không bao giờ phải viết lại lịch sử**. Tầng
> silver/gold mới gộp lại theo `occurred_at`, và chỉ thay trọn phân vùng tháng bị ảnh hưởng.

Với quy tắc này, lợi thế lớn nhất của SQLMesh gần như biến mất. Phần còn lại — hệ sinh thái,
số lượng ví dụ, `astronomer-cosmos` cho Airflow — là lợi thế về **tốc độ làm việc trong 1
tháng**, cùng loại lý do với việc chọn Airflow vì đã quen: ràng buộc ngân sách, không phải
đám đông.

**Rủi ro pháp lý đã kiểm chứng:** dbt Core v2.0 + Fusion engine vẫn Apache 2.0 sau khi
dbt Labs sáp nhập Fivetran (01/06/2026).

**Điều kiện xem lại:** nếu đến tuần 3 thấy incremental của dbt phải viết quá nhiều logic tay
để xử lý đến trễ, đổi sang SQLMesh. Logic nghiệp vụ nằm trong SQL nên chi phí chuyển vừa phải.

---

## C4 — Redeem điểm: **KHÔNG làm ở v1** ✅

**Lý do — và đây là một nhận định kiến trúc quan trọng, không chỉ là cắt phạm vi:**

> **Tích điểm an toàn khi offline. Tiêu điểm thì không.**
>
> Tích điểm là phép cộng **đơn điệu tăng** — cộng thêm dòng ledger lúc nào cũng đúng, bất kể
> có biết số dư thật hay không. Tiêu điểm cần **biết chắc số dư đủ**, mà số dư là *eventual*
> và có thể đang bị trừ ở cửa hàng khác cùng lúc. Cho phép tiêu điểm offline = cho phép tiêu
> âm.

Đây là thao tác **duy nhất** trong hệ loyalty đòi strong consistency, nên nó phá vỡ mô hình
offline-first ([NFR-01](../01-requirements.md)). Làm đúng cần một trong hai:

- **(a)** Redeem chỉ hoạt động khi trung tâm online (suy giảm rõ ràng cho khách), hoặc
- **(b)** Cấp hạn mức redeem cho từng cửa hàng theo từng khách — phức tạp đáng kể

Cả hai là việc của v2.

**Cái gì giữ sẵn:** ledger đã hỗ trợ redeem không cần đổi schema — chỉ là một dòng `delta`
âm với `reason = 'REDEEM'`. Không có nợ kỹ thuật nào phát sinh từ việc hoãn.

---

## C5 — Multi-tenant: **KHÔNG thêm `tenant_id`** ✅ *(đảo lại đề xuất trước của tôi)*

Trước đây tôi đề xuất "để sẵn cột `tenant_id` cho rẻ". **Rút lại.**

**Lý do đảo:**

1. `tenant_id` rải khắp mọi bảng làm nhiễu **mọi** truy vấn và **mọi** index, ngay từ ngày
   đầu, cho một nhu cầu chưa tồn tại.
2. Có phương án tốt hơn **không tốn gì**: **database-per-tenant**. Kiến trúc đã sẵn sàng —
   cửa hàng kết nối tới một endpoint trung tâm *có cấu hình*, nên phục vụ chuỗi thứ hai chỉ
   là dựng thêm một stack trung tâm.
3. Với bán lẻ, database-per-tenant còn **đúng hơn** về nghiệp vụ: yêu cầu cô lập dữ liệu
   giữa các chuỗi thường là ràng buộc cứng, không phải tùy chọn.

**Đánh đổi đã biết:** database-per-tenant tốn kém nếu sau này thành sản phẩm SaaS phục vụ
hàng trăm chuỗi nhỏ. Nếu đi hướng đó thì cần thiết kế lại — nhưng lúc đó schema sẽ đổi nhiều
thứ khác nữa, nên `tenant_id` để sẵn bây giờ cũng không cứu được.

---

## Q2 — Trích xuất dữ liệu: **Python thuần**, không dùng `dlt` ✅

**Lý do:** `dlt` mạnh khi có **nhiều nguồn dị loại**. Ta có **một loại nguồn duy nhất**
(Postgres) và một mẫu trích xuất duy nhất: `SELECT ... WHERE recorded_at BETWEEN :start AND
:end` → Parquet → MinIO. Ước tính ~100 dòng.

Quan trọng hơn: idempotency đòi **kiểm soát chính xác việc phân vùng và ghi đè** — thêm một
lớp trừu tượng vào giữa chỉ làm khó chỗ dễ sai nhất. Đây đúng là chỗ v1 đã hỏng.

---

## Q4 — BI: **Metabase**, ưu tiên Should ✅

Một container, người không biết SQL vẫn dùng được, driver ClickHouse ổn. Nếu tuần 4 cạn thời
gian thì thay bằng một file `analytics.sql` chứa sẵn các truy vấn mẫu — không chặn nghiệm thu.

---

## Tổng hợp hệ quả lên lộ trình

| Quyết định | Ảnh hưởng tới [06-roadmap](../06-roadmap.md) |
|---|---|
| C0 Postgres | Không đổi. Thêm việc: chỉnh autovacuum cho `outbox` ở migration đầu (ngày 2) |
| C1 HTMX + Alpine | Không cần dựng Node/bundler → tiết kiệm ~0.5 ngày ở ngày 5 |
| C3 dbt + phân vùng theo ngày nạp | **Ràng buộc thiết kế mới cho ngày 11** — bronze phân vùng theo `recorded_at` |
| C4 bỏ redeem | Tuần 2 nhẹ đi ~1 ngày → dùng đệm đó cho các kịch bản AT |
| C5 bỏ `tenant_id` | Schema gọn hơn |
| Q2 Python thuần | Bỏ ô "thử `dlt` 2 giờ" ở ngày 11 |
