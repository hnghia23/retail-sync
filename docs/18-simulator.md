# 18 — Bộ giả lập dữ liệu

> Theo [ADR-010](adr/010-data-flow-first.md), bộ giả lập **thay chỗ UI làm nguồn dữ liệu**
> trong giai đoạn A và B. Nó không chỉ sinh tải: nó là **bên biết đáp án**. Vì chính nó tạo
> ra dữ liệu, nó ghi được con số đúng, và mọi tầng của luồng ([17 §5](17-data-flow.md)) phải
> khớp con số đó.

---

## 1. Ba yêu cầu không được thỏa hiệp

1. **Đi qua đường ghi production.** Ở chế độ đầu-cuối, bộ giả lập gọi Edge API thật và use
   case thật, trong transaction thật. Không `INSERT` thẳng vào bảng giao dịch của cửa hàng
   ([ADR-010](adr/010-data-flow-first.md) quy tắc 3).
2. **Tái lập được.** Cùng `seed` + cùng profile thì ra cùng chuỗi ý định bán hàng. Một lần
   chạy lỗi có thể chạy lại y hệt để gỡ lỗi.
3. **Không tự thành nút thắt.** Tải phát theo **vòng hở** (open-loop): request tới theo lịch
   đã định, không chờ request trước trả lời. Vòng kín (đợi xong mới gửi tiếp) che mất độ trễ
   thật khi hệ thống chậm (*coordinated omission*). Bộ giả lập tự đo CPU của chính nó, và
   phép đo nào chạy khi nó vượt 50% CPU đều bị coi là **không hợp lệ**.

## 2. Vì sao cần ba chế độ — tính bằng số

| Muốn chứng minh | Khối lượng | Đi qua đường ghi thật (HTTP → edge → sync) được không? |
|---|---|---|
| Luồng đúng, tự hồi phục, ổn định 72h | T0: 3 cửa hàng × 100 đơn/ngày | ✅ Dễ dàng |
| Trung tâm chịu được nhiều cửa hàng đẩy cùng lúc | T2: 200 cửa hàng đẩy song song | ❌ 200 stack cửa hàng × ~250 MB ≈ 50 GB RAM, laptop có 16 GB |
| Warehouse chịu được dữ liệu T2 | 256 triệu dòng fact (2 năm T2) | ❌ ~73 triệu đơn; kể cả lạc quan 200 đơn/s qua HTTP cũng mất **~100 giờ**, chưa tính đồng bộ và trích xuất |
| Partition pruning của `point_ledger` | 50 triệu dòng | ❌ Cùng lý do |

→ Mỗi câu hỏi cần một đường vào khác nhau. **Nhưng cả ba chế độ dùng chung một bộ sinh**
(§4) và chung định dạng manifest (§6), nên dữ liệu có cùng hình dạng và cùng cách kiểm.

## 3. Ba chế độ

| | `edge` — đầu-cuối thật | `virtual` — cửa hàng ảo | `bulk` — lịch sử |
|---|---|---|---|
| **Đi vào ở** | Edge API (`POST /sales`, `/customers`, `/shifts/*`) | Central API `POST /events` | File Parquet ghi thẳng vào bronze (và `COPY` vào `point_ledger` cho phép đo seam) |
| **Số cửa hàng** | 1–3 stack thật trong compose | 20 → 2000, chạy trong một tiến trình | Bất kỳ |
| **Thời gian** | Thật: cửa hàng tự đóng dấu `occurred_at` | Tổng hợp, đóng vai đồng hồ của cửa hàng (trung tâm vốn tin đồng hồ cửa hàng) | Tổng hợp, nhiều tháng/năm |
| **Chạy những gì** | S1 → S6, toàn bộ | S2 (phía client) → S6 | S5 → S6 |
| **Chứng minh** | Tính đúng, AT, CH-1…5, CH-7, test ngâm, LD-1 | LD-2, LD-4, CH-6, AT-04 ở quy mô lớn, tải ingest T2/T3 | LD-3, seam 50 triệu dòng, backfill, dựng lại từ bronze |
| **Được dùng làm bằng chứng đúng?** | ✅ | ✅ (phần trung tâm trở đi) | ❌ **Chỉ đo khối lượng** |

**Chế độ `virtual` phải hành xử như một cửa hàng thật ở phía mạng:** lô ≤ `SYNC_BATCH_SIZE`,
tôn trọng `Retry-After`, lùi có jitter. Nó **dùng lại** `edge.sync.client.HttpCentralClient`
và `next_backoff`, không viết lại, để thứ được đo đúng là hành vi của worker thật. Payload được
dựng bằng domain thuần của edge (tính tiền, tính điểm) và model của `shared.events`, nên một
payload sai hợp đồng sẽ không dựng được ngay từ đầu. Mỗi cửa hàng ảo có khóa riêng, cấp bằng
`central.ops.provision_store`.

✅ **Đã hiện thực (2026-09-24)**, `packages/simulator/sinks/virtual.py`:
- `next_backoff` tách ra `edge/sync/backoff.py` (thuần) để dùng chung; envelope dựng bằng
  `shared.events.build_envelope()`, cùng hàm với outbox thật. Hợp đồng import-linter nới đúng cho
  `edge.sync.client`, `edge.sync.backoff`, `edge.pos.domain`, `edge.loyalty.domain`; một unit
  test kiểm import sink này không kéo `sqlalchemy`/`fastapi`/`asyncpg` theo.
- Cửa hàng ảo "commit" mọi thứ nó dựng, nên manifest là đáp án. Không có Postgres cửa hàng: "L1"
  là trạng thái của nó, outbox cuối ghi ở `StoreRecord.outbox`, audit bỏ qua L1 thật.
- Mạng có công tắc (transport ném `ConnectError`), nên lỗi offline đi đúng đường
  `CentralUnavailableError` của client thật. Lỗi đường truyền **không** tăng `attempts` (test
  với `max_attempts = 2` đỏ ngay nếu phá quy tắc này).
- Hạng lúc bán theo điểm MÀ CỬA HÀNG ĐÓ biết (như `customer_local`). Khách của cửa hàng khác chỉ
  dùng được khi trung tâm đã nhận `CustomerCreated` của khách đó, vì ngoài đời cửa hàng B tra
  khách qua trung tâm. Chưa tới thì bán như khách vãng lai (`foreign_fallback`).
- Ngày giả lập nằm trong quá khứ (mặc định kết thúc hôm qua), nên `recorded_at` > `occurred_at`.
- Khóa: `central.ops.provision_store --from-master N --keys-out runs/virtual-keys.env` chọn N cửa
  hàng thật chưa có khóa (không bao giờ thu hồi khóa của cửa hàng đang chạy). Chạy lại với cùng
  file thì giữ đúng các cửa hàng cũ.

**Chế độ `bulk` ghi đúng schema mà bước trích xuất S4 ghi ra.** Một test hợp đồng so schema
Parquet của hai bên. Lệch schema thì phép đo LD-3 đang đo một thứ khác với production.

✅ **Đã hiện thực (2026-09-25)**, `packages/simulator/sinks/bulk_parquet.py` + lệnh `simulator bulk`:
- Cùng `plan_store` (lịch ý định) và cùng domain của edge (`price_cart`, `resolve_tier`,
  `points_earned_for`) như chế độ `virtual` — không có bộ tính tiền thứ hai. UUIDv7 mang mốc
  `occurred_at` như cửa hàng sinh lúc bán; mọi thứ tất định theo seed (test: cùng seed → cùng
  bytes Parquet).
- Hai bước: sinh theo cửa hàng song song nhiều tiến trình (mảnh cục bộ chia theo tháng của
  `recorded_at`), rồi gộp mỗi (bảng, tháng) thành MỘT file trên lake với cửa sổ `[đầu tháng,
  đầu tháng sau)` — 24 × 7 file thay cho 120 nghìn file theo giờ. Bộ nạp và `loaded_until()`
  không phụ thuộc độ dài cửa sổ.
- **Prefix RIÊNG** `bronze/bulk-<profile>` (không bao giờ `bronze/central`), nạp bằng CHÍNH bộ nạp
  thật `python -m pipeline load` (`LAKE_PREFIX`, `CLICKHOUSE_DB=dw_bulk`, `--until` để nạp từng
  tháng như lịch production). Dữ liệu đo khối lượng không lẫn vào lake và warehouse thật.
- Master data (cửa hàng, sản phẩm...) chép từ snapshot mới nhất của lake thật; cửa hàng là N cửa
  hàng thật đầu tiên của master data, để `dim_store` có tên và vùng.
- Test hợp đồng `tests/integration/test_bulk_schema.py`: schema từng bảng == `TableSpec.schema()`
  của S4; DI-2/DI-3 và điểm đúng tại nguồn; đơn cuối tháng sang file tháng sau (bẫy 5).

## 4. Mô hình sinh dữ liệu

Mọi con số dưới đây là **giả định cấu hình được** (`simulator/profiles/*.toml`), lấy từ
[00 §Giả định](00-context.md), [02](02-scale-capacity.md) và [B01](business/01-operating-model.md).
Có dữ liệu thật thì chỉnh profile, không sửa code.

| Khía cạnh | Mặc định | Nguồn |
|---|---|---|
| Profile quy mô | `t0` 3 CH × 100 đơn/ngày · `t1` 20 × 300 · `t2` 200 × 500 · `t3` 2000 × 800 | [02 §1](02-scale-capacity.md) |
| Giờ mở cửa | 07:00–22:00 giờ VN | B01 |
| Phân bố trong ngày | Hai đỉnh: trưa 11–13h ~25%, tối 17–20h ~35% doanh thu ngày | [02 §1](02-scale-capacity.md) (đã hiệu chỉnh) |
| Trong tuần / dịp lễ | Thứ 7–CN ×1,3 · ngày lễ ×2 | B01 |
| Thời điểm đến | Quá trình Poisson theo cường độ của từng giờ | Vòng hở, §1 |
| Số dòng/đơn | 1 + Poisson(2,5), trung bình 3,5 | A1 |
| Chọn sản phẩm | Zipf trên 5556 sản phẩm thật của `product_cache` (≈ 20% mã chiếm 80% lượt). **Thứ hạng phổ biến theo giá** (rẻ bán nhiều hơn), nhiễu log-chuẩn σ = `price_rank_noise` | `crawl_data/`. Lần chạy đầu xáo ngẫu nhiên → 3,1 triệu/đơn; theo giá → ~470k/đơn, trung vị ~200k |
| Số lượng/dòng | 1 (80%), 2–5 (20%) | |
| Có khách định danh | 60% đơn | A2 |
| Khách mua nhiều cửa hàng | 10% khách có cửa hàng "phụ" trong cùng vùng | Sinh AT-04 tự nhiên |
| Khách mới/ngày | 3% số đơn có khách là đăng ký mới (`POST /customers` trước `POST /sales`) | |
| Thanh toán | Tiền mặt 70% · thẻ 20% · ví 5% · **tách** 5% | FR-P14 |
| Trả hàng | 2% đơn, trong vòng 7 ngày *(chỉ khi `ReturnItems` đã có — A-Should)* | |
| Ca | 2 ca/ngày, mở 07:00 và 14:30 | |

## 5. "Tật" của dữ liệu thật — bật được theo từng lần chạy

Dữ liệu quá sạch sẽ không lộ được lỗi mà dữ liệu thật sẽ lộ. Mỗi tật là một cờ bật/tắt, có
tần suất cấu hình được:

| Tật | Chế độ | Nhằm chặng / bẫy |
|---|---|---|
| Cửa hàng offline 5 phút → 3 ngày | `edge` (qua bộ hỗn loạn: `docker network disconnect`), `virtual` (dồn hàng đợi) | S2, CH-1 |
| Offline **vắt qua cuối tháng**, đồng bộ vào tháng sau | `virtual` | S6, [bẫy 5](17-data-flow.md) |
| Cùng SĐT đăng ký ở hai cửa hàng lúc cùng offline | cả hai | S3, C03 |
| Cùng khách mua đồng thời ở hai cửa hàng | cả hai | S3, AT-04 |
| Gửi lặp nguyên lô (`resend_share`, `resend_times` lần — CH-7: 10) | `virtual` | S3, CH-7 |
| Đồng hồ cửa hàng lệch +10 phút | `virtual` | Phát hiện lệch đồng hồ ([08 §2.1](08-reliability-and-scale.md)) |
| Sự kiện độc (`schema_version` 99), tỉ lệ rất thấp | `virtual` | Đường dead-letter |
| Nhiều cửa hàng nối lại mạng cùng một lúc | `virtual` | Backpressure, CH-6 |

## 6. Manifest và bộ đối soát

**Manifest là đáp án.** Mỗi lần chạy ghi ra `manifest/<run_id>/` gồm:

- **Tổng kỳ vọng theo `(store_id, business_date)`:** số đơn, Σ `total`, Σ thanh toán theo
  phương thức, Σ điểm.
- **Điểm kỳ vọng theo khách:** `balance`, `lifetime_earned`.
- **Danh sách đơn "không rõ kết cục":** request bị timeout hoặc mất kết nối giữa chừng, ví dụ
  khi `kill -9` ở CH-2.

Nguồn của đáp án khác nhau theo chế độ:
- `edge`: lấy từ **phản hồi `201` mà Edge API đã trả**, tức những gì cửa hàng đã cam kết với
  khách. Bộ giả lập không tự tính giá. Đơn nào đã nhận `201` thì phải có mặt ở mọi tầng.
- `virtual`, `bulk`: lấy từ chính các envelope và dòng mà bộ giả lập đã sinh.

**`simulator audit --run <id>`** so manifest với các tầng L1…L4 theo bảng ở
[17 §5](17-data-flow.md). Kết quả ghi dạng máy đọc được (JSON), để test ngâm và test hỗn loạn
dùng làm điều kiện đạt/trượt. Bộ đối soát có ba trạng thái:

| Kết quả | Nghĩa |
|---|---|
| `CONVERGED` | Mọi tầng khớp manifest |
| `CONVERGING` | Tầng sau còn thiếu so với tầng trước, nhưng **không có tầng nào thừa hay sai**, kèm độ trễ hiện tại. Bình thường khi luồng đang chảy |
| `DIVERGED` | Có tầng thừa, có số sai, hoặc đã quá hạn hội tụ mà vẫn thiếu. **Sự cố** |

Đơn "không rõ kết cục" chỉ được phép rơi vào một trong hai trạng thái: **có đủ ở mọi tầng**,
hoặc **vắng ở mọi tầng**. Có ở một nửa tầng thì là `DIVERGED`.

## 7. Điều khiển và quan sát

```bash
uv run python -m simulator run --mode edge --profile t0 --duration 2h --rate x1 --seed 42
uv run python -m simulator run --mode virtual --profile t1 --keys runs/virtual-keys.env \
    --days 3 --start-date 2026-08-30 --rate 270 --quirks offline,resend,concurrent_customer
uv run python -m simulator bulk --profile t2 --months 24 --workers 6   # → lake/bronze/bulk-t2
uv run python -m simulator audit --run <run_id>
```

- `--rate xK` nén thời gian với `virtual`/`bulk` (một ngày giả lập chạy trong 1/K ngày thật).
  Với `edge`, nó chỉ nhân cường độ, vì đồng hồ vẫn là đồng hồ thật của cửa hàng.
- Bộ giả lập gửi chỉ số và trace qua OTLP với `service.name=simulator`: request/s, tỉ lệ lỗi,
  độ trễ quan sát được phía client, CPU của chính nó. Nhờ vậy thấy được trên cùng dashboard với
  hệ thống bị thử.
- Dừng êm bằng Ctrl+C: ghi nốt manifest trước khi thoát. Manifest dở dang vẫn dùng được,
  những đơn chưa có phản hồi được xếp vào "không rõ kết cục".

## 8. Cấu trúc code

```
packages/simulator/            ✅ = đã có (2026-09-23)
├── profiles/        ✅ t0.toml · t1.toml · t2.toml · t3.toml  ← §4, chỉnh không sửa code
├── profile.py       ✅ đọc TOML, kiểm tính hợp lệ, tỉ lệ doanh số từng giờ
├── generator.py     ✅ thuần: (profile, seed, nonce, rate) → lịch ý định (mở ca, bán, đóng ca)
├── sinks/
│   ├── edge_http.py      ✅ ý định → Edge API (httpx async, vòng hở, JWT của nhân viên đã seed)
│   ├── virtual.py        ✅ ý định → envelope → HttpCentralClient (dùng lại của edge.sync)
│   └── bulk_parquet.py   ✅ ý định → Parquet đúng schema bronze (2026-09-25)
├── quirks.py        ✅ §5 — offline, resend, concurrent_customer (các tật khác: giai đoạn B)
├── manifest.py      ✅ §6 — ghi đáp án + độ trễ + CPU của chính bộ giả lập
├── audit.py         ✅ §6 — so đáp án với L1 (PG cửa hàng), L2 (PG trung tâm), L3 (bronze), L4 (mart)
└── __main__.py      ✅ CLI `run` / `bulk` / `audit`
```

- Code nằm ở **`packages/simulator/`**, không phải thư mục gốc `simulator/` như bản đầu của doc
  này: ruff, `mypy --strict`, pytest và `import-linter` đều đã trỏ vào `packages/`, nên bộ giả
  lập được kiểm như mọi code khác mà không phải cấu hình riêng.
- Kết quả chạy ghi ở `runs/<run_id>/manifest.json` và `audit.json` (gitignore).
- **Tổng tiền lấy từ `POST /sales/quote`**, không tự tính. `POST /sales` đòi thanh toán khớp đúng
  tổng tiền (INV-2), mà tổng do cửa hàng tính (chiết khấu hạng, làm tròn). Tạm tính và chốt đơn
  dùng CHUNG một hàm `_price()` ở `place_sale.py`. Bộ giả lập tự tính giá bằng bản sao logic thì
  sẽ là bộ tính tiền thứ hai, và sẽ lệch khi đổi `PricingRules`.
- **`nonce`** (mặc định ngẫu nhiên, ghi trong manifest) đổi dải SĐT giữa hai lần chạy. Chạy lại
  cùng seed trên cùng DB mà trùng SĐT thì cửa hàng trả về khách cũ (đã có điểm từ lần trước),
  và điểm kỳ vọng trong manifest sẽ sai.
- **Phạm vi đối soát là các CA của lần chạy.** Dữ liệu khác trong cùng DB (lần chạy trước, bán
  tay qua UI) không làm nhiễu kết quả. So theo từng `sale_id`, không chỉ theo tổng: mất một đơn
  và nhân đôi một đơn cùng giá thì khớp tổng nhưng vẫn là hai lỗi.
- **`5xx` ở `POST /sales` là "không rõ kết cục"**, không phải "bị từ chối" (2026-09-25, CH-2/CH-3):
  máy chủ chết GIỮA lúc chốt thì commit có thể đã xong mà phản hồi mất. Chỉ `4xx` chắc chắn chưa
  ghi gì. Xếp nhầm `5xx` vào `rejected` là bộ đối soát thấy "đơn thừa ở cửa hàng" và báo DIVERGED.
  Mở/đóng ca thử lại khi mất kết nối hay `5xx` (thu ngân bấm lại) — an toàn vì mở lần hai trả
  `409 SHIFT_ALREADY_OPEN` kèm `shift_id`, đóng lần hai trả `409`.
- Cửa hàng ảo xả xong outbox thì gửi một **heartbeat** (lô rỗng) như worker thật lúc rảnh: trung
  tâm ghi "đã bắt kịp", trễ đồng bộ về 0 (docs/13 §2).
- Đóng ca: bộ giả lập đếm két bằng đúng số tiền mặt nó đã trả qua các đơn `201`, nên
  `variance ≠ 0` (khi không có đơn không rõ kết cục) là `DIVERGED`. Nhờ vậy phép cộng tiền mặt
  của đóng ca cũng được kiểm ở mỗi lần chạy.
- `simulator` là một package trong workspace `uv`, chung ruff, `mypy --strict`, pytest.
- Đăng nhập bằng nhân viên dựng sẵn `shared.seed_data.demo_employees(store_id)`:
  `<store>-mgr-01` (đóng ca), `<store>-cash-01..02` (mở ca, đăng ký khách, bán). Nạp bằng
  `edge.ops.seed --employees` + `central.ops.seed --employees-for <store>` (`make seed-employees`),
  mật khẩu từ `SEED_EMPLOYEE_PASSWORD`.
- Vòng đời một ngày ở chế độ `edge`: `POST /shifts/open` → (`POST /customers`)* →
  `POST /sales`* → `POST /shifts/{id}/close` → ca sau. `409 SHIFT_ALREADY_OPEN` trả kèm
  `shift_id` của ca đang mở, nên bộ giả lập khởi động lại giữa chừng vẫn tiếp tục được.
- **Ranh giới import** (hợp đồng `simulator-is-a-client` + `nothing-imports-simulator`):
  bộ giả lập **không import `edge` hay `central`**, và ngược lại. Chế độ `edge` chỉ cần HTTP.
  Khi làm chế độ `virtual`, hợp đồng sẽ nới đúng cho `edge.sync.client` và domain thuần (để dựng
  payload), không hơn. Bộ giả lập là một **client**, không phải một phần của hệ thống bị thử.
- `generator.py` là hàm thuần nên test được không cần Docker: phân bố đúng, tái lập theo seed.

## 9. Công cụ tải: bộ giả lập thay `k6`

[08 §6.3](08-reliability-and-scale.md) bản cũ chọn `k6` cho LD-1…LD-4. Đã đổi (2026-09-23) vì:

- Một đơn hợp lệ cần sản phẩm có thật, ca đang mở, khách đã tồn tại, JWT của nhân viên. Viết
  lại những thứ đó bằng JavaScript cho `k6` nghĩa là **hai bộ sinh dữ liệu**, và hai bộ sẽ
  lệch nhau.
- `k6` không làm được chế độ `virtual` hay `bulk`, và không ghi được manifest để đối soát.
- Tải cần đạt vẫn nhỏ: đỉnh T3 ~260 ghi/s ở trung tâm ([02](02-scale-capacity.md)). Python
  asyncio + httpx đủ sức cho mức này. Nếu không đủ thì quy tắc 50% CPU ở §1 sẽ báo ra.

**Điều kiện xem lại:** bộ giả lập chạm trần CPU trước khi hệ thống bị thử chạm trần. Lúc đó
chạy nhiều tiến trình giả lập song song (cửa hàng ảo chia theo dải), rồi mới tới đổi công cụ.

## 10. Làm theo thứ tự nào

| Bước | Việc | Giai đoạn |
|---|---|---|
| 1 | ✅ `generator.py` + profile `t0` + test phân bố | A |
| 2 | ✅ Sink `edge_http` + manifest, chạy 1 cửa hàng | A |
| 3 | ✅ `audit` cho L1, L2 (Postgres cửa hàng + trung tâm) | A |
| 4 | ✅ Sink `virtual` + tật `offline`, `resend`, `concurrent_customer` (2026-09-24) | A |
| 5 | ✅ `audit` cho L3 (`--clickhouse`) và L4 (`--marts`), 2026-09-24 | A — điều kiện của cổng A |
| 6 | ✅ Sink `bulk` + test hợp đồng schema với bước trích xuất (2026-09-25) | B |
| 7 | ✅ Profile `t2`, `t3`; `resend_times` cho CH-7. Tật đồng hồ lệch và sự kiện độc KHÔNG làm thành tật: sự kiện độc được diễn tập thẳng qua `POST /events` (`test_alert_drills.py`), đồng hồ lệch chưa có bộ phát hiện ở trung tâm để kiểm | B |

---
*Changelog: 2026-09-25 — bước 6–7: chế độ `bulk` (prefix lake riêng, bộ nạp thật, nạp theo tháng),
profile t2/t3, `resend_times`; `5xx` ở chốt đơn là "không rõ kết cục"; heartbeat cuối của cửa hàng
ảo.*

*Changelog: 2026-09-24 — bước 4 (chế độ `virtual` + 3 tật) hiện thực; §3 ghi các quyết định
hiện thực (backoff tách module thuần, `build_envelope` dùng chung, khách ngoại chỉ khi trung tâm
đã biết, ngày giả lập trong quá khứ, cấp khóa hàng loạt).*

*Changelog: 2026-09-23 (lần 2) — bước 1–3 đã hiện thực ở `packages/simulator/` (§8 ghi vị trí mới
và lý do); tổng tiền lấy từ `POST /sales/quote`; `nonce`; phạm vi đối soát theo ca.*

*Changelog: 2026-09-23 — tạo mới theo [ADR-010](adr/010-data-flow-first.md); thay `k6` bằng
bộ giả lập cho test tải (§9).*
