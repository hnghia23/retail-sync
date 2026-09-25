# Simulator — bộ giả lập dữ liệu

**Nguồn dữ liệu của giai đoạn A và B** ([ADR-010](../../docs/adr/010-data-flow-first.md)), thay
chỗ UI. Không chỉ sinh tải: nó ghi **manifest** (đáp án đúng), và `audit` so đáp án đó với mọi
tầng của luồng dữ liệu. Thiết kế đầy đủ: [docs/18-simulator.md](../../docs/18-simulator.md).

| Chế độ | Đi vào ở | Dùng cho | Trạng thái |
|---|---|---|---|
| `edge` | Edge API thật (`/shifts/*`, `/customers`, `/sales/quote`, `/sales`) | Tính đúng, AT, hỗn loạn, test ngâm, LD-1 | ✅ |
| `virtual` | Central `POST /events` (cửa hàng ảo, 20 → 2000), tật `offline`/`resend`/`concurrent_customer` | LD-2, LD-4, CH-6, AT-04, tải ingest T2/T3 | ✅ (2026-09-24, 20 cửa hàng trên compose `CONVERGED` L0…L4) |
| `bulk` | Parquet thẳng vào bronze | LD-3, seam 50 triệu dòng. **Chỉ đo khối lượng** | ⏳ giai đoạn B |

```bash
# Nhân viên dựng sẵn (một lần): make seed-employees — mật khẩu từ SEED_EMPLOYEE_PASSWORD
SEED_EMPLOYEE_PASSWORD=... uv run python -m simulator run --profile t0 \
    --store store-001=http://localhost:8001 --days 1 --rate 60 --seed 42

uv run python -m simulator audit --manifest runs/<run_id>/manifest.json \
    --edge-dsn store-001=postgresql://edge_app:...@localhost:5433/edge_store_001 \
    --central-dsn postgresql://central_app:...@localhost:5434/central --wait 120     --clickhouse http://dw:...@localhost:8123/dw --marts    # thêm L3 (bronze) + L4 (mart)

# Chế độ virtual: cấp khóa N cửa hàng thật (SECRET, runs/ gitignore) rồi chạy
uv run python -m central.ops.provision_store --from-master 20 --keys-out runs/virtual-keys.env
uv run python -m simulator run --mode virtual --profile t1 --keys runs/virtual-keys.env     --days 3 --start-date 2026-08-30 --rate 270 --quirks offline,resend,concurrent_customer
```

`--rate 60` = một giờ mở cửa mỗi phút thật (ngày 15 giờ → 15 phút). `audit` thoát `0` khi
`CONVERGED`, `1` khi còn lại. Kết quả ở `runs/<run_id>/` (gitignore).

Quy tắc không thỏa hiệp:
- Chế độ `edge` **không** `INSERT` thẳng vào bảng giao dịch cửa hàng. Tổng tiền lấy từ
  `POST /sales/quote`, không tự tính.
- Tải phát theo vòng hở; phép đo chỉ hợp lệ khi bộ giả lập < 50% CPU (manifest ghi `cpu_share`
  và `scheduler_lag_s`).
- Là client: không import `central`, và chỉ import ĐÚNG 4 module thuần của edge
  (`edge.sync.client`, `edge.sync.backoff`, `edge.pos.domain`, `edge.loyalty.domain`) cho chế độ
  `virtual`, để cửa hàng ảo tính tiền và lùi y như cửa hàng thật (hợp đồng `import-linter` +
  test tính thuần).

Mục tiêu tải đỉnh cần tái hiện ở giai đoạn B: **~260 ghi/giây ở 2000 cửa hàng** (đã hiệu chỉnh,
[docs/02](../../docs/02-scale-capacity.md)).
