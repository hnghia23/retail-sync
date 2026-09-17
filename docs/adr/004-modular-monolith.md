# ADR-004 — Edge là modular monolith, không phải microservices

**Trạng thái:** Được chấp nhận · 2026-09-11

## Bối cảnh

v1 tách POS và Loyalty thành hai ứng dụng riêng (FastAPI và Flask), dự định chạy như hai
service. Kết quả thực tế: **hai ứng dụng không bao giờ nối được với nhau**
(`loyalty_api.py` rỗng 0 byte, `place_order.py` chỉ có `# TODO`), và không cái nào được
đóng gói vào docker-compose.

Đây là triệu chứng kinh điển: chi phí phối hợp của microservices tiêu hết ngân sách trước
khi tính năng kịp hoàn thành.

## Quyết định

Ứng dụng tại cửa hàng là **một tiến trình FastAPI duy nhất** chứa hai module: `pos` và
`loyalty`. Ranh giới giữa hai module được giữ **nghiêm ngặt như thể chúng là hai service**.

Sync worker là tiến trình riêng (vì vòng đời khác hẳn: chạy nền, không phục vụ request).

## Ranh giới module — các quy tắc

Ranh giới chỉ có giá trị nếu được cưỡng chế. Bốn quy tắc:

1. **Giao tiếp chỉ qua interface công khai.** `pos` gọi `loyalty` qua một protocol/ABC khai
   báo rõ, không gọi thẳng vào tầng trong.
2. **Không truy vấn chéo bảng.** `pos` không bao giờ `SELECT` từ bảng của `loyalty` và
   ngược lại.
3. **Không dùng chung transaction nghiệp vụ.** Ngoại lệ duy nhất: cùng ghi vào `outbox`.
4. **Model riêng.** Mỗi module có domain model của mình. Dùng chung thì đặt ở `shared/`.

```
packages/edge/
├── pos/
│   ├── domain/        # thuần, không phụ thuộc gì
│   ├── application/   # use case
│   ├── adapters/      # DB, HTTP
│   └── ports.py       # ← interface mà pos CẦN từ bên ngoài
├── loyalty/
│   ├── domain/
│   ├── application/
│   ├── adapters/
│   └── api.py         # ← interface mà loyalty CUNG CẤP
└── shared/
    ├── events.py      # schema sự kiện
    ├── outbox.py
    └── types.py
```

Cưỡng chế bằng công cụ: dùng `import-linter` trong CI để build fail nếu có import vi phạm.
**Không dựa vào kỷ luật con người** — kỷ luật sẽ trượt lúc 11 giờ đêm tuần thứ 3.

## Vì sao không microservices ở v1

| Chi phí của microservices | Ở bối cảnh này nghĩa là gì |
|---|---|
| Nhiều deployment | 2× Dockerfile, 2× healthcheck, 2× cấu hình, 2× log stream |
| Giao tiếp mạng | Lời gọi nội bộ trở thành HTTP → phải xử lý timeout, retry, circuit breaker cho việc lẽ ra là một lời gọi hàm |
| Nhất quán phân tán | "Lưu đơn + tích điểm" từ một transaction ACID trở thành saga. Đáng kể công sức |
| Gỡ lỗi | Lỗi phải lần qua ranh giới mạng thay vì một stack trace |
| Thời gian | Ước tính +1 tuần cho bằng đó tính năng |

Đổi lại được gì? Ở cửa hàng, hai module **luôn deploy cùng nhau, scale cùng nhau, chết
cùng nhau** (cùng một máy tại cửa hàng). **Không có lợi ích vận hành nào cả.**

Microservices trả lời câu hỏi "làm sao nhiều đội làm việc độc lập" và "làm sao scale từng
phần riêng". Dự án này: **một người**, và hai module có cùng một hồ sơ tải.

## Vì sao vẫn giữ ranh giới nghiêm ngặt

Nếu không tách service, tại sao phải khắt khe?

Vì **ranh giới module chính là đường cắt tương lai**. Khi nào cần tách (có team, hoặc
loyalty cần scale riêng ở tầng trung tâm), việc tách chỉ là:

1. Đổi implementation của interface từ gọi hàm sang gọi HTTP
2. Tách database
3. Deploy riêng

Nếu ranh giới đã rò rỉ — `pos` join thẳng bảng loyalty, dùng chung model — thì việc tách
biến thành viết lại.

**Modular monolith là microservices trả chậm.** Ta trả phần thiết kế bây giờ (rẻ), hoãn
phần vận hành tới khi thật sự cần (đắt).

## Trường hợp đặc biệt: trung tâm

API trung tâm cũng là một service duy nhất ở v1. Nhưng nó có hồ sơ tải khác edge và **có
thể** cần tách sớm hơn:

- Endpoint nhận sự kiện đồng bộ: ghi nhiều, chịu đợt dội
- Endpoint tra cứu khách: đọc nhiều, cần độ trễ thấp

Nếu hai cái này cạnh tranh tài nguyên ở T2+, tách chúng là bước hiển nhiên. Giữ ranh giới
module tương tự ở trung tâm để sẵn sàng.

## Phương án đã xem xét và loại

| Phương án | Vì sao loại |
|---|---|
| Hai microservice ngay từ đầu | Xem bảng chi phí trên. v1 đã chứng minh thất bại theo đúng cách này |
| Monolith không ranh giới | Nhanh nhất trong 4 tuần, nhưng khóa chặt tương lai. Chi phí của ranh giới rất thấp (chủ yếu là kỷ luật + 1 công cụ CI) nên không đáng đánh đổi |
| Loyalty là thư viện nhúng, không phải module | Gần giống lựa chọn hiện tại, nhưng thư viện khó giữ trạng thái và vòng đời riêng |
| Kiến trúc plugin | Thừa phức tạp cho 2 module |

## Hệ quả

### Tích cực
- Một tiến trình, một deployment, một log stream — gỡ lỗi dễ hơn nhiều
- "Lưu đơn + tích điểm" nằm trong **một transaction ACID** → đúng đắn miễn phí, không cần saga
- Tiết kiệm ~1 tuần
- Tách service sau này là việc cơ học, không phải viết lại

### Tiêu cực
- Không scale riêng từng module. **Không quan trọng ở edge** (một máy/cửa hàng)
- Cần công cụ cưỡng chế ranh giới, nếu không sẽ trượt
- Một bug nặng ở loyalty có thể kéo sập cả POS → **phải cô lập lỗi ở tầng code**: mọi lời
  gọi vào loyalty từ luồng bán hàng đều phải bọc try/except và suy giảm có kiểm soát

### Việc bắt buộc phải làm
- [ ] Cấu hình `import-linter` với hợp đồng ranh giới, chạy trong CI
- [ ] Định nghĩa interface `LoyaltyPort` trước khi viết code nghiệp vụ
- [ ] Mọi lời gọi loyalty trong luồng bán hàng đều có fallback (bán được cả khi loyalty lỗi)
