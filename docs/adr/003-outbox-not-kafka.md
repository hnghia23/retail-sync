# ADR-003 — Đồng bộ bằng Transactional Outbox + HTTP ở v1, Kafka để sau

**Trạng thái:** Được chấp nhận · 2026-09-11

## Bối cảnh

v1 có Kafka + Zookeeper trong docker-compose, và `loyalty_service` publish event trực tiếp
trong request handler. Nhưng `producer.py` và `consumer.py` là **file rỗng 0 byte** — không
ai consume. Kafka tồn tại trong hạ tầng mà không tồn tại trong hệ thống.

Ngoài ra, cách publish của v1 có một lỗi nghiêm trọng:

```python
session.commit()  # ghi DB xong
publish_event(...)  # nếu chết ở đây → event mất vĩnh viễn
```

Đây là **dual-write problem**: ghi hai nơi không trong cùng transaction, không có cách nào
đảm bảo cả hai cùng thành công.

## Quyết định

v1 dùng **Transactional Outbox + worker đẩy HTTP**. Không dùng Kafka.

Nhưng thiết kế outbox sao cho **Kafka là bước nâng cấp tự nhiên, không phải viết lại**.

## Cách hoạt động

### Ghi: outbox trong cùng transaction

```python
async with db.begin():                    # MỘT transaction
    await db.execute(insert(Sale)...)
    await db.execute(insert(SaleLine)...)
    await db.execute(insert(PointLedger)...)
    await db.execute(insert(Outbox).values(
        event_id=uuid4(),
        event_type="SaleCompleted",
        payload={...},
        created_at=now(),
    ))
# COMMIT — hoặc tất cả cùng thành công, hoặc không gì cả
```

Dual-write problem biến mất: outbox nằm **trong cùng database**, cùng transaction với dữ
liệu nghiệp vụ.

### Đọc: worker riêng biệt

```sql
SELECT * FROM outbox
WHERE  sent_at IS NULL
ORDER  BY id
LIMIT  100
FOR UPDATE SKIP LOCKED;     -- nhiều worker chạy song song không giẫm chân nhau
```

Worker POST lô lên trung tâm, trung tâm dedupe theo `event_id`, worker đánh dấu `sent_at`.

Lỗi → backoff lũy thừa + jitter. Quá N lần → dead-letter + cảnh báo.

## Vì sao không Kafka ngay

| Lý do | Chi tiết |
|---|---|
| **Không giải quyết vấn đề gốc** | Kafka **không** sửa được dual-write. Vẫn cần outbox để đảm bảo "ghi DB và phát event là nguyên tử". Outbox là nền, Kafka chỉ là phương tiện vận chuyển |
| **Sai mô hình triển khai ở edge** | Kafka ở cửa hàng nghĩa là mỗi cửa hàng chạy một broker (nặng), hoặc giữ kết nối bền tới Kafka trung tâm (mà kết nối này chính là thứ hay đứt). Outbox trên Postgres cục bộ đã bền sẵn |
| **Chi phí tài nguyên** | Kafka + Zookeeper ≈ 2 GB RAM. Trên laptop chạy 3 cửa hàng mô phỏng, đó là 40% ngân sách |
| **Chi phí thời gian** | Ước tính ~1 tuần để dựng, viết producer/consumer, xử lý offset, schema registry, rebalance. Trong ngân sách 4 tuần, đó là 25% |
| **Chưa có tải để biện minh** | Ở T2 (200 cửa hàng) tổng ~4 events/giây. Kafka làm được hàng triệu. Dư 250 000 lần |

## Vì sao vẫn là đường lên Kafka

Đây là phần quan trọng — chọn outbox **không phải** vì né tránh, mà vì nó là **on-ramp**
đúng:

```
v1:   Ứng dụng → bảng outbox → worker HTTP → API trung tâm → Postgres
                    │
                    │  (đổi chỗ này, không đổi ứng dụng)
                    ▼
v2:   Ứng dụng → bảng outbox → Debezium CDC → Redpanda → consumer → Postgres
```

**Code ứng dụng không đổi một dòng.** Nó vẫn chỉ ghi vào bảng outbox. Thay đổi duy nhất là
ai đọc bảng đó.

Đây là lý do outbox tốt hơn cả việc "dùng Kafka ngay": nếu publish thẳng vào Kafka từ
application code, sau này muốn đổi gì cũng phải sửa application.

### Điều kiện kích hoạt nâng cấp

Chuyển sang CDC + Kafka/Redpanda khi **một trong các điều sau** xảy ra:
- Vượt ~100 cửa hàng
- Worker không kịp xả outbox sau sự cố mạng diện rộng
- Cần nhiều consumer khác nhau cho cùng luồng sự kiện (VD: vừa lưu DB, vừa cảnh báo realtime)
- Cần warehouse cập nhật T+0

Ước tính: ~1 tuần. Dùng **Redpanda** thay Kafka (tương thích API, một binary, không cần
Zookeeper, nhẹ hơn nhiều).

## Phương án đã xem xét và loại

| Phương án | Vì sao loại |
|---|---|
| Publish thẳng Kafka từ app | Dual-write problem. Mất event khi app chết giữa commit và publish |
| Kafka + outbox ngay từ v1 | Đúng về kiến trúc nhưng tốn 1 tuần + 2GB RAM cho lợi ích bằng 0 ở quy mô hiện tại |
| Postgres logical replication | Hấp dẫn (dùng sẵn của Postgres) nhưng ghép chặt schema cửa hàng với trung tâm — hai bên không tiến hóa độc lập được. Outbox cho ta **hợp đồng sự kiện rõ ràng** |
| Cửa hàng đẩy trực tiếp khi bán (fire-and-forget) | Mất dữ liệu khi mạng đứt. Vi phạm NFR-03 |
| Trung tâm chủ động kéo từ DB cửa hàng | Trung tâm phải biết địa chỉ và schema từng cửa hàng, và cửa hàng thường nằm sau NAT. Đảo ngược chiều kết nối sai |
| RabbitMQ / NATS | Vẫn là dual-write nếu không có outbox. Nếu đã có outbox rồi thì HTTP đủ đơn giản hơn |

## Hệ quả

### Tích cực
- Không mất event, kể cả khi app crash giữa chừng
- Không cần hạ tầng message broker ở v1 — tiết kiệm 2 GB RAM và 1 tuần
- Dễ gỡ lỗi: outbox là một bảng SQL, `SELECT * FROM outbox` là xem được ngay
- Nhiều worker song song sẵn nhờ `SKIP LOCKED`
- Đường nâng cấp không đụng application code

### Tiêu cực
- Worker phải polling (mặc định 2 giây) → độ trễ cơ bản ~2 giây. Chấp nhận được với
  NFR-04 (hội tụ < 5 phút). Có thể giảm bằng `LISTEN/NOTIFY` nếu cần
- Bảng outbox cần **job dọn dẹp** (xóa dòng đã gửi cũ hơn N ngày), nếu không sẽ phình
- Không có replay lịch sử như Kafka — nhưng lake bronze đã đóng vai trò đó

### Việc bắt buộc phải làm
- [ ] Job dọn outbox định kỳ
- [ ] Bảng và quy trình xử lý dead-letter
- [ ] Metrics: độ sâu outbox, tuổi event cũ nhất chưa gửi → cảnh báo khi vượt ngưỡng
- [ ] Test AT-02 (ngắt mạng) và AT-03 (gửi lặp)
