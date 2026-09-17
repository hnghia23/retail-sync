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

# ── Hạ tầng ──
.PHONY: up
up:  ## Bật edge + central
	$(COMPOSE) --profile edge --profile central up -d

.PHONY: up-data
up-data:  ## Bật nền tảng dữ liệu (MinIO, ClickHouse, Airflow)
	$(COMPOSE) --profile data up -d

.PHONY: up-obs
up-obs:  ## Bật observability (ADR-009 — chỉ khi cần săn bottleneck)
	$(COMPOSE) --profile observability up -d

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

# ── Quan sát — ADR-009 (cần make up-obs / make up trước) ──
.PHONY: traces
traces:  ## Liệt kê service đã gửi trace tới Grafana/Tempo — spike ngày 0, S5
	curl -s -u admin:admin http://localhost:3001/api/datasources/proxy/uid/tempo/api/search/tag/service.name/values

.PHONY: pg-stat-edge
pg-stat-edge:  ## Top 20 truy vấn chậm nhất ở DB cửa hàng
	docker exec retail-sync-edge-db-store-001-1 psql -U edge_app -d edge_store_001 -c "SELECT calls, round(mean_exec_time::numeric,2) AS mean_ms, round(total_exec_time::numeric,2) AS total_ms, left(query,80) AS query FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;"
