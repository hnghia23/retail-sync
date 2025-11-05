# retail-sync

Bộ công cụ đồng bộ dữ liệu bán lẻ (retail-sync)

Mô tả ngắn
---------------
`retail-sync` là một dự án ETL/streaming nhỏ phục vụ việc thu thập, tiền xử lý và đồng bộ dữ liệu bán lẻ từ các cửa hàng vào kho dữ liệu trung tâm. Dự án gồm các thành phần chính: dịch vụ thu thập cửa hàng (`store`), dịch vụ xử lý trung tâm (`central`), thư viện dùng chung và các notebooks hỗ trợ thu thập dữ liệu.

Mục lục
---------
- Giới thiệu
- Kiến trúc & Thành phần
- Cấu trúc thư mục
- Yêu cầu
- Cài đặt & Chạy nhanh (PowerShell)
- Phát triển & Gỡ lỗi
- Dữ liệu và persistent volumes
- Góp phần & Liên hệ
- License

Kiến trúc & Thành phần (chi tiết)
-------------------------------
Mục tiêu kiến trúc:
- Thu thập dữ liệu bán hàng từ nhiều cửa hàng (edge) theo thời gian thực hoặc theo batch.
- Tiền xử lý, làm sạch và chuyển đổi dữ liệu trước khi lưu vào kho dữ liệu phân tích.
- Hệ thống dễ mở rộng, có thể chạy local bằng Docker Compose cho dev và deploy lên môi trường container/cluster cho production.

Thành phần chính:
- `Store (edge)` — thư mục `store/`
	- Mô phỏng POS/endpoint tại cửa hàng. Thu thập dữ liệu đơn hàng, trạng thái, sự kiện POS.
	- Gửi dữ liệu lên trung tâm bằng HTTP/gRPC hoặc đẩy vào message broker (nếu cấu hình).
	- Chạy độc lập cho mỗi cửa hàng; có thể scale theo số lượng cửa hàng.

- `Central (ingest & ETL)` — thư mục `central/`
	- Chịu trách nhiệm nhận dữ liệu, lưu tạm, xử lý theo DAG (mô phỏng Airflow), và nạp vào kho dữ liệu (ClickHouse, Cassandra, v.v.).
	- Có thể bao gồm component: API ingest, worker xử lý, scheduler DAGs và task runners.

- `Data Stores` — thư mục `data/` chứa volumes cho databases
	- Cassandra: lưu dữ liệu trạng thái/transaction cần khả năng ghi cao và phân tán.
	- ClickHouse: dùng cho phân tích OLAP, báo cáo thời gian thực.

- `libs/`
	- Thư viện dùng chung (domain models, adapters, use_cases) để tránh trùng lặp logic giữa `store` và `central`.

- `Auxiliary services`
	- Các service phụ trợ như `pos_service/`, `loyalty_service/` cung cấp ví dụ cách tích hợp tính năng miền.

Luồng dữ liệu (data flow) — mô tả sơ đồ
1) Tại cửa hàng (`store`) dữ liệu đơn hàng sinh ra (event).
2) `store` gửi event tới endpoint ingest ở `central` (hoặc publish tới broker).
3) `central` nhận, xác thực, ghi tạm (buffer) và enqueue task xử lý.
4) Worker thực hiện ETL: validate, enrich (vd: join thông tin cửa hàng, product mapping), và persist vào Cassandra/ClickHouse.
5) Jobs định kỳ (DAGs) thực hiện aggregation, cleanup, xuất báo cáo.

ASCII diagram (đơn giản):

```
 [Store (many)] --> [Central API Ingest] --> [Buffer/Queue] --> [Workers/ETL] --> [Cassandra / ClickHouse]
												 |                                              ^
												 v                                              |
										[Monitoring / Logs] -------------------------------+
```

Giao diện & Protocols
- Ingest API: HTTP(S) JSON hoặc gRPC (tùy cấu hình). Payload nên có trace_id/timestamp để dễ debug.
- Worker <-> DB: sử dụng official drivers (Cassandra, ClickHouse).
- Internal comms: REST/gRPC hoặc message broker (Kafka/Redis Streams) nếu cần throughput lớn.

Ports & Compose mapping (ví dụ dev)
- `central` API: 8000
- `store` demo: 5000
- Cassandra: 9042
- ClickHouse: 8123 / 9000

Khả năng mở rộng & Triển khai
- Dev: Docker Compose (đã có `central/docker-compose.yml` và `store/docker-compose.yaml`).
- Staging/Prod: containerize từng service và deploy lên Kubernetes / ECS / Docker Swarm.
- Scale:
	- Tăng số instance `store` để mô phỏng nhiều cửa hàng.
	- Scale worker pool ngang (replica) để xử lý throughput cao.
	- Sử dụng message broker để tách ingest và processing nhằm tăng độ bền và khả năng backpressure.

Resilience & Fault Tolerance
- Sử dụng queue/buffer để tránh mất dữ liệu khi downstream bị chậm.
- Lưu temporary events vào disk/volume trước khi ack nếu cần đảm bảo at-least-once.
- Thực hiện retry/backoff cho các thao tác truy vấn DB và gửi mạng.

Observability
- Logs: cấu hình logs cho mỗi service (stdout -> docker logs). Gợi ý forward logs vào ELK/Prometheus + Grafana cho production.
- Metrics: expose Prometheus metrics từ API/worker (request latencies, queue length, processed count).
- Tracing: thêm trace_id trong payload để theo dõi end-to-end.

Bảo mật
- Tất cả API ingest nên xác thực (API key / JWT) khi ra production.
- Dữ liệu nhạy cảm (PII) phải mã hóa khi lưu hoặc mask trước khi gửi ra analytics.

Cấu hình & môi trường
- Các giá trị cấu hình (DB URL, credentials, API keys) lưu ở environment variables hoặc secrets store.
- Trong Docker Compose dev, đặt các biến trong `.env` (không commit).

Lưu ý vận hành
- Backup các DB quan trọng (Cassandra snapshot / ClickHouse backup).
- Giám sát disk/IO, vì ClickHouse và Cassandra có yêu cầu I/O mạnh.
- Sơn màu path Windows/OneDrive: tránh mount dữ liệu DB trực tiếp vào OneDrive để giảm rủi ro corruption.

Tích hợp CI/CD (gợi ý)
- Pipeline build image, chạy lint/tests, đẩy image lên registry.
- Pipeline deploy cho staging/prod (kubectl/helm/az-cli/gitops...).

Phần mở rộng
- Nếu muốn, mình có thể bổ sung sơ đồ kiến trúc có hình (mermaid/PlantUML) và hướng dẫn triển khai Kubernetes (manifests/Helm charts).


Cấu trúc thư mục (tóm tắt)
--------------------------
Ví dụ một số thư mục chính:

- `central/` - DAGs, docker-compose cho môi trường trung tâm
- `store/` - service cửa hàng, docker-compose
- `libs/` - thư viện nội bộ
- `data/` - dữ liệu persistent (Cassandra, ClickHouse,...)
- `plugins/`, `logs/` - plugin và log cho runtime
- `crawl_data/` - notebooks & script thu thập dữ liệu từ web

Yêu cầu
--------
- Docker & Docker Compose
- Python 3.8+ (nếu chạy scripts/notebooks cục bộ)
- (Tùy chọn) Cassandra / ClickHouse nếu muốn chạy full stack cục bộ

Cài đặt & Chạy nhanh (PowerShell)
----------------------------------
Từ root repository trên Windows PowerShell, ví dụ các lệnh khởi chạy môi trường dev:

1) Khởi dịch vụ trung tâm (central):

```powershell
cd central; docker-compose up -d
```

2) Khởi dịch vụ cửa hàng (store):

```powershell
cd store; docker-compose up -d
```

3) Kiểm tra logs (ví dụ):

```powershell
docker-compose -f central\docker-compose.yml logs -f
docker-compose -f store\docker-compose.yaml logs -f
```

Ghi chú: Nếu bạn sử dụng OneDrive/Windows paths dài, hãy đảm bảo Docker có quyền truy cập vào thư mục workspace và path không quá dài (Windows MAX_PATH).

Phát triển & Gỡ lỗi
-------------------
- Chạy unit tests (nếu có): thêm command test tương ứng (pytest/unittest) vào `store` hoặc `central` khi cần.
- Chạy notebooks: mở `*.ipynb` bằng Jupyter/VS Code.
- Khi sửa code trong `libs/`, mount volume hoặc restart container để load thay đổi.

Dữ liệu & Persistent volumes
----------------------------
- Thư mục `data/` chứa dữ liệu cho các DB cục bộ (Cassandra, ClickHouse...). Không commit dữ liệu sản xuất lên git.
- Backup/restore: tuỳ theo DB, dùng snapshot/backup chính thức (Cassandra snapshot, ClickHouse dump).

Góp phần
--------
1. Fork repository
2. Tạo branch feature/bugfix
3. Mở pull request với mô tả rõ ràng

Vui lòng tuân thủ coding style hiện có và thêm test cho thay đổi logic quan trọng.

Liên hệ
-------
Nếu cần trợ giúp, mở issue trên repository hoặc liên hệ trực tiếp với người maintain dự án (thông tin liên hệ / Slack/Email nếu có trong org).

License
-------
Mặc định chưa có file license trong repository. Nếu bạn muốn public, cân nhắc thêm `LICENSE` (MIT/Apache-2.0) và cập nhật phần này.

Ghi chú cuối
------------
README này cung cấp hướng dẫn nhanh để bắt đầu làm việc với `retail-sync`. Nếu bạn cần mình mở rộng phần cấu trúc chi tiết của từng service, ví dụ file cấu hình Docker Compose, cách chạy DAGs, hoặc tạo script seed dữ liệu — cho mình biết, mình sẽ bổ sung.

