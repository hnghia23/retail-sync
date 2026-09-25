# Tiến độ 2026-09-24 (lần 3) — giai đoạn A: bộ giả lập chế độ `virtual` + 3 tật

> Nhật ký tiến độ, không phải docs thiết kế. Nối tiếp
> [2026-09-24-giai-doan-a-dbt-airflow.md](2026-09-24-giai-doan-a-dbt-airflow.md) §5 mục 1.
> Đặc tả: [docs/18 §3, §5](../18-simulator.md).

**Kết quả chính:** 20 cửa hàng ảo × 3 ngày (30/8–1/9), bật cả ba tật của cổng A, chạy trên
compose thật → **`CONVERGED` ở L0…L4 cho từng cửa hàng, từng ngày**: 19.930 đơn,
9.982.240.541đ, 552.445 điểm. Bẫy 5 được chứng minh trên compose. Lần chạy cũng lộ ra **hai lỗi
thật** mà các lần chạy `edge` không thể lộ.

## 1. Đã làm

- **`packages/simulator/sinks/virtual.py`**: N cửa hàng ảo trong một tiến trình, đẩy thẳng lên
  `POST /events`. Tính tiền/điểm bằng domain thật của edge, payload bằng model thật của
  `shared.events`, xả outbox bằng `HttpCentralClient` + `next_backoff` thật. Mạng có công tắc
  (transport ném `ConnectError`).
- **`packages/simulator/quirks.py`**: `offline` (khoảng giờ cửa hàng, chỉ tính giờ mở cửa),
  `resend` (gửi lại nguyên lô đã được nhận), `concurrent_customer` (khách của A mua ở B gần cùng
  lúc với một lần mua ở A). Tham số chỉnh bằng `--quirk-arg`.
- Dùng chung thay vì chép:
  - `next_backoff` → `edge/sync/backoff.py` (thuần);
  - `shared.events.build_envelope()`, dùng cho cả outbox thật lẫn cửa hàng ảo;
  - `payment_split` dùng chung cho hai chế độ.
- Hợp đồng import-linter nới **đúng** cho 4 module thuần. Một unit test kiểm import sink
  `virtual` không kéo `sqlalchemy`/`fastapi`/`asyncpg` theo.
- Audit: cửa hàng ảo không có Postgres, nên "L1" là trạng thái của nó trong manifest (outbox cuối
  ở `StoreRecord.outbox`). `expected_points` cộng điểm của khách trên mọi cửa hàng.
- `central.ops.provision_store --from-master N --keys-out FILE`: chọn N cửa hàng thật chưa có
  khóa, không bao giờ thu hồi khóa của cửa hàng đang chạy; chạy lại với cùng file thì giữ đúng
  các cửa hàng cũ. `make provision-virtual`, `make sim-virtual`.

## 2. Lỗi thật lần chạy này lộ ra

| # | Lỗi | Hậu quả | Sửa | Test |
|---|---|---|---|---|
| 1 | **Không có partition `point_ledger` cho tháng trước.** `ensure_point_ledger_partitions()` chỉ tạo tháng này + 3 tháng tới. DB trung tâm của compose (tạo 18/9) không có `point_ledger_2026_08` | Cửa hàng offline qua cuối tháng, đồng bộ ngày 1 → điểm của tháng trước bị từ chối **không thử lại** (23514) → vào dead-letter; đơn vẫn vào. Khách mất điểm, âm thầm. Chỉ xảy ra ở tháng go-live và mọi môi trường mới (staging, DR) — đúng lúc khó thấy nhất | Migration `0006`: `months_back` (mặc định 1). Lùi xa hơn vẫn từ chối ồn ào | `test_last_month_is_accepted_on_a_fresh_database`; test `virtual` đỏ trước khi sửa (76 sự kiện điểm dead-letter) |
| 2 | **Ca chỉ lên trung tâm khi đóng** (không có sự kiện mở ca). DAG chạy lúc cửa hàng đang bán → mọi đơn của ca đang mở trỏ tới ca trung tâm chưa biết | Test `relationships` fact → `dim_shift` đỏ → **DAG thất bại, chặn luồng** mỗi lần chạy trong giờ bán | `dim_shift` có dòng inferred (`is_closed = 0`) cho ca thấy trong đơn; ca đóng thì lượt sau có dòng thật. Ca đang mở là bình thường nên không cảnh báo | `test_sale_in_a_still_open_shift_does_not_break_the_build`, **đột biến 1/1** |

Vì sao chế độ `edge` không lộ được hai lỗi này: lỗi 1 cần `occurred_at` ở tháng trước, mà cửa hàng
thật chỉ cho khai ngày hôm nay/hôm qua. Lỗi 2 cần DAG chạy giữa lúc đang bán, mà trước đây DAG
luôn chạy sau khi mọi ca đã đóng.

## 3. Kiểm chứng

- **Test:** 7 unit (tật, đồng hồ tổng hợp, file khóa, tính thuần của import) + 2 tích hợp
  `virtual` trên Central API thật (đủ ba tật → `CONVERGED`; đối chứng: xóa một đơn ở trung tâm →
  audit bắt được) + 1 test schema + 1 test dbt.
- **Kiểm đột biến 2/2:**
  - Tính lỗi đường truyền là một lượt thử: test `virtual` đỏ, vì `max_attempts = 2` + offline
    → dead-letter.
  - Bỏ ca inferred: test dbt đỏ.
  - Lỗi partition thì chính lần chạy đầu đã tái hiện.
- **Compose, `live-v-t1-1`** (profile `t1`, `--rate 270`, ~10 phút thật):
  - 31.904 sự kiện được nhận, 0 còn chờ, **0 dead-letter**.
  - 5 cửa hàng offline 18:00 31/8 → 12:00 1/9, gặp 121 lỗi đường truyền.
  - 479 lô bị gửi lại, **0 lệch** (CH-7).
  - 563 đơn mua chéo cửa hàng; 11 lần bán như khách vãng lai vì khách chưa tới trung tâm.
  - **Bẫy 5:** DAG lần 1 chạy giữa lúc offline: mart 31/8 của `…-0005` có 164/308 đơn. Sau khi
    cửa hàng xả outbox, DAG lần 2 dựng lại phân vùng `202608` → 308/308.
  - Audit L0…L4 `CONVERGED` cho cả 20 cửa hàng. INV-4 (`central.ops.reconcile`): 649 khách,
    **drift 0**.
- **Tín hiệu tải (ghi cho giai đoạn B, chưa xử lý):**
  - `POST /events` phía client có p50 86 ms, p95 643 ms, **p99 1,65 s**, và một request chạm đúng
    timeout 10 s. Cảnh: 20 cửa hàng đẩy song song, mỗi lô được nhận đều bị gửi lại một lần,
    tất cả trên một laptop.
  - Ngưỡng ở [17 §6](../17-data-flow.md) là p95 < 1 s (vẫn đạt), nhưng đuôi dài cần săn bằng OTel
    trước test tải T2.
  - CPU của bộ giả lập 6,9% (dưới quy tắc 50%).

## 4. Trạng thái cuối

- Toàn repo **395/395** test (383 → 395). ruff, `mypy --strict`, **12/12** hợp đồng import sạch.
  Link docs sạch.
- DB trung tâm compose đã lên migration `0006`. 20 cửa hàng thật (`thanh-pho-can-tho-0001…0020`)
  có khóa trong `runs/virtual-keys.env` (secret, gitignore) và nhân viên dựng sẵn ở trung tâm.
- Airflow đã trả về cửa sổ 1 giờ mặc định. Chưa commit gì.

## 5. Còn lại của cổng A

> Cả hai xong cùng ngày: [2026-09-24-cong-a.md](2026-09-24-cong-a.md).

1. `t0` 3 cửa hàng `edge` chạy **2 giờ liên tục** → `CONVERGED` L0…L4 (cần thêm 2 stack cửa hàng
   vào compose).
2. AT-01…04 thành `tests/scenarios/`.

Sáu mục còn lại của cổng A đã xong ([06 §Cổng A](../06-roadmap.md)).

---
*Viết bởi Claude (Opus 5.5) theo các lần kiểm chứng thật trong phiên 2026-09-24.*
