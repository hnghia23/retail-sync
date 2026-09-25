# Lệnh thường dùng. Windows: chạy qua Git Bash, hoặc gõ thẳng lệnh `uv run ...`.
.DEFAULT_GOAL := help
COMPOSE := docker compose -f infra/compose.yaml

.PHONY: help
help:  ## Danh sách lệnh
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n",$$1,$$2}'

# ── Phát triển ──
.PHONY: install
install:  ## Cài dependency (cần uv: https://docs.astral.sh/uv/)
	uv sync --all-extras --dev

.PHONY: fmt
fmt:  ## Format code
	uv run ruff format .
	uv run ruff check --fix .

.PHONY: lint
lint:  ## ruff + mypy + ranh giới module (ADR-004)
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy
	uv run lint-imports

.PHONY: test
test:  ## Test nhanh — không cần Docker
	uv run pytest tests/unit

.PHONY: test-all
test-all:  ## Toàn bộ test (cần Docker cho integration)
	# Ryuk (dọn rác testcontainers) có race lúc khởi động, đã gặp nhiều lần khi phát
	# triển cục bộ — tắt hẳn, tự dọn container bằng tay khi cần (docker rm -f ...).
	TESTCONTAINERS_RYUK_DISABLED=true uv run pytest

.PHONY: test-scenarios
test-scenarios:  ## AT-01..04, AT-07, AT-10, DI-1..3 tren COMPOSE THAT (tat central-api trong AT-02!). Stack dung bang make bootstrap
	RETAIL_SYNC_SCENARIOS=1 uv run pytest tests/scenarios -v

# ── Hạ tầng ──
.PHONY: bootstrap
bootstrap:  ## Dung stack tu con so 0 (migrate, seed, cap khoa, lake, cho DAG) - chay lai duoc. OBS=1 them observability
	uv run python infra/bootstrap.py $(if $(OBS),--observability,)

.PHONY: up
up:  ## Bật edge + central
	$(COMPOSE) --profile edge --profile central up -d

.PHONY: up-data
up-data:  ## Bật nền tảng dữ liệu (MinIO, ClickHouse, Airflow 3 — UI http://localhost:8081)
	$(COMPOSE) --profile data up -d --build

.PHONY: up-obs
up-obs:  ## Bat observability: otel-lgtm + flow-monitor. Dashboard "Suc khoe luong": http://localhost:3001 (docs/17 s6)
	$(COMPOSE) --profile observability up -d --build

.PHONY: up-all
up-all:  ## Toàn bộ stack — dùng để nghiệm thu, đo RAM (spike S1)
	$(COMPOSE) --profile edge --profile central --profile data --profile observability up -d

.PHONY: down
down:  ## Tắt mọi service (GIỮ dữ liệu)
	$(COMPOSE) --profile edge --profile central --profile data --profile bi --profile observability down

.PHONY: ram
ram:  ## Đo RAM thật của stack — spike ngày 0, S1 (đạt khi <= 8 GB)
	docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"

# ── Migration ──
.PHONY: migrate-edge
migrate-edge:  ## Chạy migration DB cửa hàng (cần DATABASE_URL)
	uv run alembic -c packages/edge/alembic.ini upgrade head

.PHONY: migrate-central
migrate-central:  ## Chạy migration DB trung tâm (cần DATABASE_URL)
	uv run alembic -c packages/central/alembic.ini upgrade head

# ── Seed dữ liệu thật từ crawl_data/ (roadmap tuần 1 ngày 4) ──
.PHONY: seed-central
seed-central:  ## Nạp region/store/product từ crawl_data/ vào DB trung tâm (cần DATABASE_URL)
	uv run python -m central.ops.seed

.PHONY: seed-edge
seed-edge:  ## Nạp product_cache từ crawl_data/ cho MỘT cửa hàng (cần DATABASE_URL, STORE_ID)
	uv run python -m edge.ops.seed

# ── Đồng bộ cửa hàng → trung tâm (ADR-003, roadmap tuần 2) ──
.PHONY: provision-store
provision-store:  ## Đăng ký cửa hàng + cấp khóa sync (cần DATABASE_URL trỏ DB trung tâm, STORE_ID)
	uv run python -m central.ops.provision_store --store-id $(or $(STORE_ID),store-001)

.PHONY: seed-employees
seed-employees:  ## Nhan vien dung san cho bo gia lap (can SEED_EMPLOYEE_PASSWORD, EDGE_DATABASE_URL, CENTRAL_DATABASE_URL)
	DATABASE_URL=$(EDGE_DATABASE_URL) uv run python -m edge.ops.seed --employees
	DATABASE_URL=$(CENTRAL_DATABASE_URL) uv run python -m central.ops.seed --employees-for $(or $(STORE_ID),store-001)

.PHONY: sim-run
sim-run:  ## Bo gia lap che do edge vao compose (SEED_EMPLOYEE_PASSWORD: bien moi truong hoac infra/.env). PROFILE, DAYS, RATE, SEED
	uv run python -m simulator run --profile $(or $(PROFILE),t0) --store store-001=http://localhost:8001 --days $(or $(DAYS),1) --rate $(or $(RATE),60) --seed $(or $(SEED),42)

.PHONY: provision-virtual
provision-virtual:  ## Cap khoa N cua hang that cho che do virtual -> runs/virtual-keys.env (SECRET; can DATABASE_URL). N
	uv run python -m central.ops.provision_store --from-master $(or $(N),20) --keys-out runs/virtual-keys.env

.PHONY: sim-virtual
sim-virtual:  ## Bo gia lap che do virtual (20 cua hang ao -> POST /events). PROFILE, DAYS, START, RATE, QUIRKS
	uv run python -m simulator run --mode virtual --profile $(or $(PROFILE),t1) --keys runs/virtual-keys.env --days $(or $(DAYS),3) $(if $(START),--start-date $(START),) --rate $(or $(RATE),270) --seed $(or $(SEED),42) --quirks $(or $(QUIRKS),offline,resend,concurrent_customer)

.PHONY: sim-audit
sim-audit:  ## Doi soat MANIFEST L0-L2 (EDGE_DSN, CENTRAL_DSN); them CLICKHOUSE=http://u:p@host:8123/dw de co L3+L4; WATCH=300 lap lai; OTLP=http://localhost:4318 day len dashboard
	uv run python -m simulator audit --manifest $(MANIFEST) --edge-dsn store-001=$(EDGE_DSN) --central-dsn $(CENTRAL_DSN) --wait $(or $(WAIT),120) $(if $(CLICKHOUSE),--clickhouse $(CLICKHOUSE) --marts,) $(if $(WATCH),--watch $(WATCH),) $(if $(OTLP),--otlp $(OTLP),)

.PHONY: reconcile
reconcile:  ## Doi soat INV-4 tang dan o trung tam (can DATABASE_URL). FULL=1 de quet toan bo
	uv run python -m central.ops.reconcile $(if $(FULL),--full,)

.PHONY: pipeline-init
pipeline-init:  ## Tao bucket lake + bang bronze_* (can bien PIPELINE_*/LAKE_*/CLICKHOUSE_*, xem infra/.env.example)
	uv run python -m pipeline init

.PHONY: pipeline-run
pipeline-run:  ## S4+S5: trich moi cua so da an toan -> bronze Parquet -> ClickHouse
	uv run python -m pipeline run

.PHONY: flow-health
flow-health:  ## Suc khoe luong MOT lan (S3-S6, do tuoi L2/L4) in JSON - cung so voi dashboard (bien PIPELINE_*/LAKE_*/CLICKHOUSE_*)
	uv run python -m pipeline health

.PHONY: dbt-build
dbt-build:  ## S6: dbt build (staging -> dim/fact + test) vao ClickHouse (can CLICKHOUSE_PASSWORD; venv rieng qua uvx)
	uvx --python 3.12 --with-requirements data_platform/requirements-dbt.txt --from dbt-core dbt build --project-dir data_platform/dbt --profiles-dir data_platform/dbt $(ARGS)

.PHONY: dag-run
dag-run:  ## Kich hoat DAG retail_pipeline ngay (S4 -> S5 -> S6), khong doi lich
	docker exec retail-sync-airflow-scheduler-1 airflow dags trigger retail_pipeline

.PHONY: sync-status
sync-status:  ## Outbox cửa hàng (tồn đọng/dead-letter) + độ trễ đồng bộ ở trung tâm
	docker exec retail-sync-edge-db-store-001-1 psql -U edge_app -d edge_store_001 -c "SELECT count(*) FILTER (WHERE sent_at IS NULL AND dead_lettered_at IS NULL) AS pending, count(*) FILTER (WHERE dead_lettered_at IS NOT NULL) AS dead, count(*) FILTER (WHERE sent_at IS NOT NULL) AS sent, EXTRACT(EPOCH FROM now() - min(created_at) FILTER (WHERE sent_at IS NULL AND dead_lettered_at IS NULL))::int AS oldest_pending_s FROM outbox;"
	docker exec retail-sync-central-db-1 psql -U central_app -d central -c "SELECT store_id, status, lag_seconds, last_event_at FROM store_sync_status ORDER BY store_id;"

# ── Quan sát — ADR-009 (cần make up-obs / make up trước) ──
.PHONY: traces
traces:  ## Liệt kê service đã gửi trace tới Grafana/Tempo — spike ngày 0, S5
	curl -s -u admin:admin http://localhost:3001/api/datasources/proxy/uid/tempo/api/search/tag/service.name/values

.PHONY: pg-stat-edge
pg-stat-edge:  ## Top 20 truy vấn chậm nhất ở DB cửa hàng
	docker exec retail-sync-edge-db-store-001-1 psql -U edge_app -d edge_store_001 -c "SELECT calls, round(mean_exec_time::numeric,2) AS mean_ms, round(total_exec_time::numeric,2) AS total_ms, left(query,80) AS query FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;"
