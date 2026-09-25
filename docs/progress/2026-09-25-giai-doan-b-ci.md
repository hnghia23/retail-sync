# Tiến độ 2026-09-25 (lần 2) — Giai đoạn B, việc 2: CI chạy `tests/scenarios/`

> Nhật ký tiến độ, không phải docs thiết kế. Nối tiếp
> [2026-09-25-giai-doan-b-dashboard.md](2026-09-25-giai-doan-b-dashboard.md). Ngưỡng CI:
> [16 §6](../16-test-plan.md).

**Xong phần có thể làm trên máy này.** Job CI `scenarios` đã viết, và toàn bộ những gì nó chạy đã
chạy thật trên một stack compose dựng **từ con số 0** (project riêng, volume mới, dữ liệu tổng hợp).
Job chưa chạy trên GitHub Actions: chưa có commit/push nào.

## 1. Vướng mắc: dữ liệu master không nằm trong git

`crawl_data/products/*` và `crawl_data/store_info/*` bị `.gitignore` từ thời v1. Một bản clone
sạch (CI, hay người mới) không có sản phẩm hay cửa hàng nào để seed, nên cả "CI chạy scenarios"
lẫn điều kiện cổng B "`git clone` → chạy được theo README" đều vỡ ở đúng chỗ này. Dữ liệu là danh
sách cửa hàng/sản phẩm crawl từ website một chuỗi bán lẻ có thật, nên **không tự ý commit**. Việc
đưa nó vào repo là quyết định của chủ dự án.

**Cách làm:** bộ dữ liệu **tổng hợp** `tests/fixtures/crawl_data/`, cùng định dạng, đọc bằng chính
parser production: 251 sản phẩm (83 mã dạng barcode, một mã trùng để đi nhánh hậu tố `-2`), 3 vùng
(một vùng không có file cửa hàng), 32 cửa hàng (đủ cho 20 cửa hàng ảo + AT-04). Có `crawl_data/`
thật thì mọi lệnh dùng dữ liệu thật; không có thì dùng bộ tổng hợp.

## 2. `infra/bootstrap.py` — một lệnh dựng stack

`uv run python infra/bootstrap.py` (= `make bootstrap`). Các bước, mỗi bước chạy lại được: DB +
MinIO + ClickHouse → migration 4 Postgres → seed master → Central API + khóa sync → nhân viên dựng
sẵn + sản phẩm cửa hàng → lake + bronze → cả stack, chờ Edge/Central API và DAG.

Ba quyết định:
- **Khóa sync chỉ cấp khi trung tâm không còn nhận khóa đang có** (hỏi bằng `POST /events []`:
  xác thực chạy, không ghi gì). Cấp lại là thu hồi khóa cũ, nên cấp ở mỗi lần chạy sẽ làm sync
  worker đang chạy nhận `401`. Đã kiểm: chạy lại lần 2 → "giữ khóa đang có" cho cả 3 cửa hàng và 20
  cửa hàng ảo.
- **`SEED_EMPLOYEE_PASSWORD` sống trong file env.** Chưa có thì sinh ngẫu nhiên và ghi vào đó.
  Trước đây mật khẩu truyền tay qua biến môi trường rồi không lưu ở đâu (phiên dashboard không
  chạy được chế độ `edge` vì thế). Bộ giả lập và `tests/scenarios/` đọc biến môi trường trước,
  không có thì đọc file env. ⚠️ Chạy bootstrap trên stack cũ mà file env chưa có mật khẩu thì nó
  seed lại nhân viên bằng mật khẩu mới.
- **Chọn được stack**: `--project`, `--env-file`, `--virtual-keys`, `--crawl-data`. `tests/scenarios/`
  đọc các biến tương ứng (`RETAIL_SYNC_COMPOSE_PROJECT`, `RETAIL_SYNC_ENV_FILE`,
  `RETAIL_SYNC_VIRTUAL_KEYS`, `RETAIL_SYNC_CRAWL_DATA`) thay cho tên container và đường dẫn viết
  cứng. Nhờ vậy mới kiểm được trên một stack mới mà không đụng dữ liệu của stack dev.

## 3. Kiểm chứng: stack mới dựng `retail-sync-ci`

Dừng stack dev (volume giữ nguyên), dựng `retail-sync-ci` từ volume trống, dữ liệu tổng hợp:
bootstrap xong trong một lệnh (32 cửa hàng, 251 sản phẩm, 3 + 20 khóa, 14 lệnh DDL, DAG đã đọc).

Lần chạy `tests/scenarios/` đầu tiên: **5/7**. Hai lỗi, đúng loại mà job CI sinh ra để bắt, vì
chúng chỉ lộ trên stack mới:

| Test | Nguyên nhân | Sửa |
|---|---|---|
| AT-07 `test_pipeline_idempotent_on_rerun` | Test chạy 03:54 → 04:04, vắt qua mốc giờ: cửa sổ `[03:00, 04:00)` đóng giữa hai lượt DAG, lượt sau có 31 dòng mới **hợp lệ**. Test giả định "không có dòng mới" mà không kiểm tiền đề đó — trên stack dev nó xanh vì trùng thời điểm | Một lượt bắt kịp trước; chỉ so chặt khi mép "đã nạp tới" không đổi giữa hai lượt; đổi thì thử cặp khác (tối đa 3) |
| AT-01 `test_normal_sale_with_known_customer` | Audit mở kết nối Postgres cửa hàng, port-forward của Docker Desktop reset kết nối đầu (`WinError 64`) → cả lần đối soát chết. ClickHouse đã có thử lại từ cổng A, Postgres thì chưa | `_pg_connect`: thử lại lỗi đường truyền (tối đa 4, lùi lũy thừa); lỗi xác thực ném ngay. Unit test cả hai nhánh |

Sau khi sửa: chạy lại bootstrap trên cùng stack (giữ mọi khóa), rồi `tests/scenarios/` **7/7 xanh**
(7 phút 21 giây).

`python -m pipeline health` chạy trong mạng compose (bước CI): 3 nguồn không lỗi, 36 chỉ số.

## 4. Job CI

`.github/workflows/ci.yml` → job `scenarios` (sau `unit` + `integration`, timeout 60 phút):
bootstrap bằng dữ liệu tổng hợp → `pytest tests/scenarios -v` → `pipeline health` → log compose
khi đỏ → `down -v`. `infra/.env` sinh từ `.env.example` (máy dùng một lần).

## 5. Quyết định của chủ dự án (2026-09-25, sau khi đọc nhật ký này)

- `crawl_data/` là dữ liệu bán hàng thật, **được dùng khi test hệ thống trên máy local**. Nó vẫn
  nằm ngoài git (repo `hnghia23/retail-sync` là public). `infra/bootstrap.py` và bộ giả lập tự
  dùng nó khi có; bộ tổng hợp `tests/fixtures/crawl_data/` chỉ dành cho clone sạch/CI.
- **Chưa đẩy lên GitHub, dự án chạy local.** Job CI `scenarios` giữ sẵn cho lúc đẩy lên; trong lúc
  chờ, "cổng CI" là chạy tay trên máy: `make bootstrap` rồi `make test-scenarios`.

## 6. Chưa làm / còn mở

- **Job chưa chạy trên GitHub.** Rủi ro còn lại là khác biệt môi trường runner: build image
  Airflow (~5–8 phút, chưa cache), RAM runner (stack ~3,5 GiB, vừa cả runner 7 GB).
- Log `docker logs` của app đầy lỗi export OTLP khi không bật profile `observability` (15 s một
  dòng metric + trace). Vô hại, nhưng làm nhiễu khi đọc log. Cân nhắc chỉ đặt
  `OTEL_EXPORTER_OTLP_ENDPOINT` khi bật profile đó.
- `test_virtual_stores_with_all_gate_a_quirks_converge_at_central` (integration) từng đỏ một lần
  dưới tải nặng — theo dõi trên CI.
