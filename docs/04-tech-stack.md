# 04 — Tech Stack

Mỗi lựa chọn ở đây phải trả lời được ba câu: **Vì sao chọn? Đã loại gì? Khi hết dư địa thì
đổi sang đâu?**

---

## 1. Bảng tổng hợp

| Tầng | Công nghệ | Vì sao | Scale seam |
|---|---|---|---|
| Ngôn ngữ | **Python 3.12** | Một ngôn ngữ cho cả API, pipeline, dbt. Solo dev không đủ ngân sách cho hai hệ sinh thái | Chỉ viết lại module nóng bằng Go nếu thật sự cần (khó xảy ra) |
| Quản lý gói | **uv** | Nhanh hơn pip 10–100×, lockfile chuẩn, thay luôn pyenv/virtualenv | — |
| API framework | **FastAPI** | Async, tự sinh OpenAPI, validate bằng Pydantic | — |
| Validate/config | **Pydantic v2 + pydantic-settings** | Một mô hình cho cả DTO, cấu hình và biên dữ liệu | — |
| DB cửa hàng | **PostgreSQL 18** | Xem [ADR-001](adr/001-postgres-everywhere.md) | SQLite nếu 1 quầy; Citus nếu quá lớn |
| Cache cửa hàng | **Redis 7** | Cache khách + phiên đăng nhập. **Không sống còn** | Redis Cluster (khó xảy ra) |
| DB trung tâm | **PostgreSQL 18** | Xem [ADR-001](adr/001-postgres-everywhere.md) | PgBouncer → read replica → Citus/CockroachDB |
| Truy cập DB | **SQLAlchemy 2.0 (async) + Alembic** | ORM chín muồi, migration có version | — |
| Đồng bộ | **Transactional Outbox + HTTP** | Xem [ADR-003](adr/003-outbox-not-kafka.md) | Debezium CDC → Redpanda, code không đổi |
| Lake | **MinIO + Parquet** (phân vùng Hive) | S3 API, chạy local, bất biến, nén tốt | Đổi endpoint sang S3/GCS; nâng lên Iceberg khi cần schema evolution |
| Warehouse | **ClickHouse** | Xem [ADR-005](adr/005-clickhouse-warehouse.md) | Thêm shard hoặc ClickHouse Cloud |
| Biến đổi | **dbt-core + dbt-clickhouse** | Test, lineage, docs, incremental — miễn phí. Đòn bẩy cao nhất trong cả stack. Đã kiểm chứng: dbt Core v2.0 + **Fusion engine (viết lại bằng Rust) vẫn open source Apache 2.0** sau khi dbt Labs sáp nhập Fivetran (01/06/2026) | SQLMesh nếu incremental phức tạp lên; dbt Cloud nếu cần managed |
| Điều phối | **Airflow 3 + LocalExecutor** | Chủ dự án đã có kinh nghiệm. Xem [ADR-007](adr/007-airflow-over-dagster.md) | CeleryExecutor → k8s executor → Astronomer/MWAA |
| dbt ↔ Airflow | **astronomer-cosmos** | Render mỗi dbt model thành một task Airflow, giữ được lineage. Bù lại lợi thế `dagster-dbt` | — |
| Trích xuất | **dlt** *(hoặc Python thuần)* | Tự lo tăng dần, schema evolution, quản lý state | — |
| BI | **Metabase** | Cài 1 container, người không biết SQL vẫn dùng được | Superset nếu cần nâng cao |
| Xác thực | **JWT (PyJWT) + Argon2** | Chuẩn, stateless, hợp với edge | Keycloak nếu cần SSO |
| Log | **structlog** → JSON | `trace_id` xuyên hệ thống | Loki/ELK |
| **Tracing** | **OpenTelemetry** (auto-instrumentation) | 🔴 **Must** — công cụ duy nhất trả lời *thời gian đi đâu* trong hệ phân tán. [ADR-009](adr/009-observability-stack.md) | Đổi endpoint OTLP sang Grafana Cloud / LGTM tách rời / SigNoz — **không sửa code** |
| **Backend giám sát** | **`grafana/otel-lgtm`** *(1 container)* | OTel Collector + Prometheus + Tempo + Loki + Grafana trong một image. SigNoz cần 4–8 GB — quá nặng | Chỉ dùng dev/test; production đổi backend |
| Metrics | **prometheus-client** *(đi kèm otel-lgtm)* | 🔴 **Must** — metrics nói *có vấn đề*, traces nói *ở đâu* | — |
| **Profiling DB** | **`pg_stat_statements`** · ClickHouse **`system.query_log`** | Tracing không nói *vì sao* truy vấn chậm. Cả hai gần như miễn phí | — |
| Test | **pytest + testcontainers** | Test bằng DB thật, không mock | — |
| Chất lượng code | **ruff + mypy** | Nhanh, một công cụ thay cho black/isort/flake8 | — |
| Đóng gói | **Docker multi-stage + Compose profiles** | Bật/tắt từng phần trên laptop | Helm khi lên k8s |
| CI | **GitHub Actions** | lint → type → test → build | — |

## 2. So sánh với stack v1

### Những thứ bỏ đi

| Bỏ | Lý do | Tiết kiệm |
|---|---|---|
| **MySQL** | Postgres làm được mọi thứ MySQL làm, và thêm: `FOR UPDATE SKIP LOCKED` (cần cho outbox), logical replication, JSONB, CHECK constraint, partial index. Dùng hai RDBMS = hai phương ngữ SQL, hai công cụ backup, hai mô hình tư duy — không đổi lại được gì | ~1 GB RAM, vài ngày học |
| **Cassandra** | Dư 25–100× so với tải đỉnh T3. Đổi lại: mất transaction, mất join, eventual consistency cho **dữ liệu tài chính**, tối thiểu 3 node. Xem [ADR-001](adr/001-postgres-everywhere.md) | ~2 GB RAM, ~1 tuần |
| **Kafka + Zookeeper** | Ở quy mô này outbox+HTTP bền tương đương và đơn giản hơn nhiều. Kafka là *đích đến*, không phải *điểm xuất phát*. Xem [ADR-003](adr/003-outbox-not-kafka.md) | ~2 GB RAM, ~1 tuần |
| ~~**Airflow**~~ → **giữ lại** | Chủ dự án đã có kinh nghiệm. Giảm nhẹ bằng **LocalExecutor** (5 container thay vì 7, bỏ Celery worker + Redis) và **tách compose profile**. Xem [ADR-007](adr/007-airflow-over-dagster.md) | ~0.8 GB nhờ LocalExecutor |
| **Flask** (loyalty service) | Thống nhất một framework | Bớt một hệ sinh thái |

### Cân đo tài nguyên

| | Stack v1 | Stack v2 |
|---|---:|---:|
| Số hệ thống hạ tầng | 8 | 5 |
| RAM ước tính (3 cửa hàng) | ~10.2 GB | ~5 GB |
| Container | ~15 | ~10 |
| Phương ngữ SQL phải biết | 4 (MySQL, PG, CQL, ClickHouse) | 2 (PG, ClickHouse) |
| Thời gian dựng hạ tầng | ~2 tuần | ~4 ngày |

**Bốn ngày tiết kiệm được ở hạ tầng chính là bốn ngày dồn vào nghiệp vụ** — phần thực sự
tạo ra giá trị của POC.

## 3. Ghi chú về những lựa chọn gây tranh cãi

### Vì sao vẫn giữ Redis dù Postgres đã đủ nhanh?

Thành thật: ở T1–T2, Postgres một mình thừa sức. Redis kiếm chỗ đứng nhờ ba việc khác:
lưu phiên đăng nhập, rate limiting, và giảm tải lookup khi mạng chập chờn. Tốn 64MB và
khoảng 30 dòng code. **Nhưng phải thiết kế sao cho tắt Redis hệ thống vẫn chạy** (AT-08).

### Vì sao ClickHouse chứ không phải DuckDB?

DuckDB rất hấp dẫn cho POC: không cần server, một file, SQL tuyệt vời. Nhưng nó **nhúng
trong tiến trình** — không phục vụ được nhiều người dùng BI đồng thời, không có server để
Metabase kết nối bền vững. Sẽ phải chuyển đổi ở T2. ClickHouse là server ngay từ đầu,
nhẹ tương đương trên laptop, và không cần di trú sau này.

*(DuckDB vẫn rất hữu ích cho việc khám phá dữ liệu ad-hoc trên Parquet trong lake — dùng
nó như một công cụ phân tích, không phải warehouse.)*

### Vì sao Parquet thuần chứ không phải Iceberg ngay?

Iceberg là đích đến đúng (schema evolution, time travel, ghi đồng thời, hoán đổi engine
truy vấn). Nhưng nó thêm một tầng khái niệm và một bộ công cụ trong khi ngân sách chỉ có
1 tháng. Parquet phân vùng Hive giải quyết 90% nhu cầu v1.

**Đường nâng cấp**: `pyiceberg` đọc được layout Parquet hiện có; chuyển đổi là thêm
metadata, không phải viết lại dữ liệu. Ghi vào roadmap v2.

### Vì sao không dùng managed service ngay?

Vì ràng buộc hiện tại là "chạy Docker trên laptop cá nhân". Nhưng mọi lựa chọn đều có bản
managed tương ứng, di trú không đau:

| Local | Managed tương ứng |
|---|---|
| PostgreSQL | AWS RDS / Cloud SQL / Neon / Supabase |
| MinIO | S3 / GCS / R2 *(cùng API)* |
| ClickHouse | ClickHouse Cloud |
| Dagster | Dagster+ |
| Redis | ElastiCache / Upstash |
| Redpanda *(v2)* | Confluent Cloud / MSK |

Đây là một tiêu chí chọn có chủ đích: **mọi thành phần đều có đường lên cloud mà không
phải viết lại.**

## 4. Cấu trúc thư mục

```
retail-sync/
├── docs/                      # ← bộ tài liệu này (nguồn sự thật cho thiết kế)
├── packages/
│   ├── shared/                # hợp đồng sự kiện, config nghiệp vụ, outbox, tracing, db
│   ├── edge/                  # app tại cửa hàng — modular monolith (ADR-004)
│   │   ├── pos/               #   domain · application (ports.py) · adapters
│   │   ├── loyalty/           #   domain · application · adapters · api.py  ← cửa duy nhất
│   │   ├── reporting/         #   lớp truy vấn A — đọc thẳng PG cửa hàng
│   │   ├── sync/              #   outbox worker (tiến trình riêng)
│   │   └── migrations/        #   Alembic — schema cửa hàng
│   └── central/               # app trung tâm
│       ├── ingest/            #   nhận sự kiện + chốt chặn idempotency
│       ├── lookup/            #   tra khách + master data
│       ├── reporting/         #   lớp truy vấn B
│       ├── ops/               #   job vận hành (bảo trì partition, đối soát)
│       └── migrations/        #   Alembic — schema trung tâm
├── data_platform/
│   ├── airflow/dags/          # định nghĩa DAG (ADR-007)
│   ├── dbt/                   # staging → intermediate → marts
│   └── seeds/                 # master data tĩnh
├── simulator/                 # sinh dữ liệu + mô phỏng nhiều cửa hàng (LD-1..4, CH-6)
├── infra/
│   ├── compose.yaml           # profile: edge | central | data | bi | observability
│   ├── .env.example
│   └── docker/                # app.Dockerfile (dùng chung 3 service) + airflow.Dockerfile
├── tests/                     # unit · integration · scenarios · load — xem docs/16
└── store/, central/           # ← code v1, CHỈ để tham khảo (docs/09-v1-postmortem.md)
```

Ranh giới module được cưỡng chế bằng `import-linter` (`.importlinter`, 7 hợp đồng) trong CI.

## 5. Quyết định còn treo

**Không còn.** Toàn bộ đã chốt 2026-09-11 — xem [07-stack-decision.md](07-stack-decision.md)
và [ADR-008](adr/008-remaining-decisions.md).

Ba thay đổi so với bản đầu của doc này:

| Mục | Bản đầu | Đã chốt | Vì sao đổi |
|---|---|---|---|
| Điều phối | Dagster | **Airflow 3 + LocalExecutor** | Chủ dự án đã có kinh nghiệm; Airflow 3.2 đã có asset partitioning |
| UI | *(treo)* | **HTMX + Alpine.js** | Tránh toolchain thứ hai |
| Multi-tenant | Để sẵn `tenant_id` | **Không thêm cột** | Database-per-tenant tốt hơn, không tốn gì bây giờ |

---
*Changelog: 2026-09-17 — §4 đổi từ "cấu trúc dự kiến" sang cấu trúc thật đang có trong repo
(bỏ `legacy/` chưa dùng, `data_platform/dagster/` → `airflow/dags/`, thêm `central/ops/`).*

*Changelog: 2026-09-11 — tạo mới.*
