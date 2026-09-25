"""Nền tảng dữ liệu — chặng S4 (trích xuất → bronze Parquet) và S5 (bronze → ClickHouse).

docs/17-data-flow.md. Chạy được ở hai nơi: task Airflow (image riêng, data_platform/
requirements.txt) và pytest/CLI trong workspace. Vì vậy package này KHÔNG import `shared`,
`edge` hay `central` (hợp đồng `import-linter`): phụ thuộc của nó chỉ là asyncpg, pyarrow,
httpx. Nó đọc Postgres trung tâm bằng SQL như mọi consumer phân tích khác.
"""
