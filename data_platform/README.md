# Nền tảng dữ liệu (lớp C, T+1)

```
airflow/dags/     DAG — Airflow 3 LocalExecutor (ADR-007)
dbt/              staging → intermediate → marts (ADR-005)
seeds/            master data tĩnh (dim_date, danh mục)
requirements.txt  dependency của image Airflow — tách khỏi pyproject.toml vì Airflow ghim
                  constraint rất chặt, trộn chung sẽ kéo tụt version FastAPI/SQLAlchemy ở edge
```

Trích xuất viết bằng **Python thuần**, không dùng `dlt` (ADR-008 Q2).

Bốn ràng buộc chi phối tầng này là #3 (bronze phân vùng theo `recorded_at`), #4
(`insert_deduplication_token`), #5 (`fact_payment` tách riêng) và #8 (đối soát tăng dần) —
đọc ở [CLAUDE.md](../CLAUDE.md) và [docs/05 §7](../docs/05-data-model.md).
