# B03 — Workload phân tích thực tế

Doc này liệt kê **các truy vấn hệ thống thực sự phải phục vụ**, đo khối lượng của từng loại,
rồi đối chiếu với năng lực từng engine. Đây là cơ sở để trả lời câu hỏi *"Postgres hay
ClickHouse?"* bằng số, không bằng cảm tính.

> **Phát hiện chính:** thiết kế hiện tại **bỏ sót hoàn toàn một lớp truy vấn** — và đó lại là
> lớp có tần suất cao nhất trong vận hành thật.

---

## 1. Ba lớp truy vấn

Sai lầm phổ biến (mà thiết kế v2 hiện tại đang mắc) là gom mọi thứ "phân tích" vào một chỗ.
Thực tế có **ba lớp khác nhau về bản chất**:

| | **Lớp A — Vận hành** | **Lớp B — Quản lý cửa hàng** | **Lớp C — Phân tích chuỗi** |
|---|---|---|---|
| Ai dùng | Thu ngân, quản lý CH | Quản lý CH, quản lý khu vực | Trụ sở: mua hàng, marketing, BGĐ |
| **Độ tươi bắt buộc** | **T+0 (tức thì)** | T+0 tới T+1 | T+1 chấp nhận được |
| Phạm vi | 1 cửa hàng, 1 ngày/ca | 1 cửa hàng, 1–12 tháng | Toàn chuỗi, 12–24 tháng |
| Kích thước kết quả | Vài dòng | Vài chục–trăm dòng | Vài chục–nghìn dòng |
| **Tần suất** | **Rất cao** — mỗi ca, mỗi cửa hàng | Trung bình | Thấp — vài lần/ngày |
| Tính chọn lọc | Rất cao (index lookup) | Cao | Thấp (quét rộng) |

**Lớp A không được đi qua warehouse.** Quản lý cửa hàng cần số của *hôm nay*, không phải của
*hôm qua sau khi ETL chạy*. Một pipeline T+1 về mặt định nghĩa là **không thể** phục vụ lớp này.

## 2. Danh mục truy vấn và khối lượng

### Lớp A — Vận hành (T+0 bắt buộc)

| Truy vấn | Tần suất | Dòng quét (T3) | Engine đúng |
|---|---|---:|---|
| Báo cáo chốt ca của cửa hàng này | 2–3 × / CH / ngày | ~900 | Postgres |
| Doanh thu hôm nay của cửa hàng này | ~10 × / CH / ngày | ~2 800 | Postgres |
| Tra hóa đơn cũ để trả hàng | vài × / CH / ngày | 1 (index) | Postgres |
| Lịch sử điểm của khách đang đứng ở quầy | theo yêu cầu | ~20 | Postgres |
| Tồn kho sản phẩm X tại cửa hàng này | cao | 1 (index) | Postgres |
| Doanh thu theo thu ngân trong ca | mỗi lần chốt ca | ~900 | Postgres |

**Ở mọi bậc quy mô, lớp này quét dưới ~3 000 dòng.** Không có kịch bản nào cần cột-lưu.
Đây là truy vấn OLTP có gộp nhóm nhẹ, và **phải chạy trên DB cửa hàng** để còn hoạt động khi
mất mạng.

### Lớp B — Quản lý cửa hàng

| Truy vấn | Dòng quét T1 | T2 | T3 |
|---|---:|---:|---:|
| 30 ngày của 1 cửa hàng, theo ngày | 31 500 | 52 500 | 84 000 |
| 365 ngày của 1 cửa hàng | 383 250 | 638 750 | 1 022 000 |
| Top 20 sản phẩm tháng này tại CH này | ~52 000 | ~52 000 | ~84 000 |

**Postgres thoải mái ở mọi bậc.** Vì phạm vi luôn bị giới hạn bởi một cửa hàng, khối lượng
không tăng theo số cửa hàng.

### Lớp C — Phân tích toàn chuỗi

| Truy vấn | T0 | T1 | T2 | T3 |
|---|---:|---:|---:|---:|
| Fact thô, 12 tháng | 383 K | 7.7 Tr | **128 Tr** | **2.0 Tỷ** |
| Fact thô, 24 tháng | 767 K | 15.3 Tr | **256 Tr** | **4.1 Tỷ** |
| Rollup ngày × cửa hàng, 24 tháng | 2 K | 15 K | 146 K | 1.5 Tr |
| Rollup ngày × CH × ngành hàng, 24 tháng | 110 K | 730 K | 7.3 Tr | 73 Tr |
| Rollup ngày × CH × sản phẩm, 24 tháng | 4.4 Tr | 29 Tr | **292 Tr** | **2.9 Tỷ** |

## 3. Ngưỡng chịu được của Postgres

Tốc độ gộp nhóm thực tế của PostgreSQL (có phân vùng + parallel worker, cột được chọn lọc):
**~3–5 triệu dòng/giây**.

| Ngân sách thời gian | Dòng tối đa |
|---|---:|
| 3 giây *(dashboard tương tác)* | **~12 triệu** |
| 10 giây *(báo cáo chấp nhận chờ)* | **~40 triệu** |

## 4. Đối chiếu — engine nào cho bậc nào

| Truy vấn | T0 (POC) | T1 (20 CH) | T2 (200 CH) | T3 (2000 CH) |
|---|---|---|---|---|
| Lớp A | ✅ PG | ✅ PG | ✅ PG | ✅ PG |
| Lớp B | ✅ PG | ✅ PG | ✅ PG | ✅ PG |
| Lớp C — rollup ngày × CH | ✅ PG | ✅ PG | ✅ PG | ✅ PG |
| Lớp C — rollup × ngành hàng | ✅ PG | ✅ PG | ✅ PG (7.3 Tr) | 🟡 PG chậm (73 Tr) |
| Lớp C — rollup × sản phẩm | ✅ PG | 🟡 PG (29 Tr) | ❌ **cần CH** | ❌ **cần CH** |
| Lớp C — fact thô 24 tháng | ✅ PG | 🟡 PG ~4s (15 Tr) | ❌ **cần CH** | ❌ **cần CH** |
| Market basket *(self-join)* | ✅ PG | ❌ **cần CH** | ❌ **cần CH** | ❌ **cần CH** |

### Kết luận bằng số

> **Trực giác của bạn đúng: ở quy mô POC và pilot, PostgreSQL phục vụ được *mọi* truy vấn,
> kể cả phân tích toàn chuỗi.**
>
> ClickHouse chỉ thật sự cần khi **fact vượt ~15–40 triệu dòng cần quét tự do** — tức khoảng
> **60–100 cửa hàng** (giữa T1 và T2).
>
> Ở **T0 — chính là quy mô của POC** — toàn bộ dữ liệu chỉ ~767 nghìn dòng. ClickHouse ở đây
> **không phục vụ một truy vấn nào mà Postgres không làm được**, và làm nhanh không kém.

### Nhưng có hai ngoại lệ Postgres không cứu được

1. **Market basket analysis** (sản phẩm hay mua cùng nhau) — self-join trên `sale_id`, chi phí
   bùng nổ theo bình phương. Vượt tầm Postgres ngay từ T1. ClickHouse xử lý bằng `groupArray`.
2. **Phân tích tự do / khám phá** — khi người phân tích muốn cắt lát dữ liệu thô theo chiều
   bất kỳ mà không phải chờ ai dựng rollup. Đây là nhu cầu **tổ chức**, xuất hiện ở T2+, không
   phải nhu cầu kỹ thuật ở T1.

## 5. Lỗ hổng lớn nhất của thiết kế hiện tại

Đối chiếu danh mục trên với [03-architecture.md](../03-architecture.md) và
[06-roadmap.md](../06-roadmap.md):

| Lớp | Tần suất thực tế | Thiết kế v2 hiện tại |
|---|---|---|
| **A — Vận hành** | **Cao nhất** — mỗi ca, mỗi cửa hàng, mỗi ngày | ❌ **Không có gì cả** |
| B — Quản lý CH | Trung bình | 🟡 Ngụ ý qua warehouse — nhưng warehouse là T+1, không phục vụ được phần cần T+0 |
| C — Phân tích chuỗi | Thấp nhất | ✅ Toàn bộ tuần 3 dành cho lớp này |

**Toàn bộ tuần 3 của lộ trình phục vụ lớp truy vấn có tần suất thấp nhất, trong khi lớp có
tần suất cao nhất chưa được thiết kế.**

Đây chính là loại sai lệch mà bài tập này sinh ra để phát hiện. Nó không lộ ra khi suy luận
từ kiến trúc — chỉ lộ ra khi hỏi *"ai thực sự bấm nút, bao nhiêu lần một ngày?"*

### Cần bổ sung: lớp báo cáo vận hành

```
Báo cáo lớp A  →  đọc thẳng Postgres CỬA HÀNG  (T+0, chạy được khi offline)
Báo cáo lớp B  →  đọc Postgres TRUNG TÂM        (T+0 cho hôm nay, gộp nhiều CH)
Phân tích lớp C →  Lake → Warehouse               (T+1)
```

Lớp A **bắt buộc** nằm ở cửa hàng — nếu không, mất mạng là quản lý không chốt được ca, và
chốt ca là nghiệp vụ bắt buộc hằng ngày (xem [B02 Z01](02-edge-cases.md)).

---
*Changelog: 2026-09-11 — tạo mới. Số liệu là ước lượng, cần kiểm chứng ở spike ngày 0.*
