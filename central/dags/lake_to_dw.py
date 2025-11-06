from airflow import DAG
from airflow.decorators import task
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from datetime import datetime, timedelta
import pandas as pd
import os
from airflow.decorators import dag, task 

import clickhouse_connect

# =====================================
# Default args
# =====================================
default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "start_date": datetime(2025, 1, 1),
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

# =====================================
# DAG định nghĩa
# =====================================
@dag(
    dag_id="load_pos_to_dw",
    default_args=default_args,
    schedule_interval=None,
    catchup=False,
    tags=["dw", "clickhouse", "pos"],
)
def load_pos_to_dw():

    @task
    def extract_from_minio(store_name: str, date_str: str):
        """Tải file raw từ MinIO"""
        s3_hook = S3Hook(aws_conn_id="minio_conn_id")
        bucket = "pos"
        key = f"raw/{store_name}/{date_str}.parquet"
        local_path = f"/tmp/{store_name}_{date_str}.parquet"

        # tải file về local
        s3_hook.get_key(key, bucket_name=bucket).download_file(local_path)
        print(f"✅ Downloaded from s3://{bucket}/{key}")
        return local_path

    @task
    def transform_to_fact(local_path: str, store_name: str):
        """Chuyển dữ liệu raw thành bảng fact_sales chuẩn"""
        df = pd.read_parquet(local_path)

        # Bóc tách từng sản phẩm (product_id_1, quantity_1, ...)
        product_cols = [col for col in df.columns if col.startswith("product_id_")]
        quantity_cols = [col for col in df.columns if col.startswith("quantity_")]

        # Chuyển dạng wide → long
        melted = []
        for i in range(1, len(product_cols) + 1):
            temp = df[[
                "transaction_id", "employee_id", "customer_id", "created_at",
                "payment_method", "discount", "final_amount", "point_changed"
            ]].copy()
            temp["product_id"] = df[f"product_id_{i}"]
            temp["quantity"] = df[f"quantity_{i}"]
            melted.append(temp)

        df_long = pd.concat(melted).dropna(subset=["product_id"])
        df_long["store_id"] = store_name
        df_long["created_at"] = pd.to_datetime(df_long["created_at"])

        print(f"✅ Transformed {len(df_long)} rows into fact_sales format")
        out_path = f"/tmp/fact_sales_{store_name}.parquet"
        df_long.to_parquet(out_path, index=False)
        return out_path


    @task
    def load_to_clickhouse(fact_path: str):
        """
        Ghi dữ liệu vào ClickHouse sử dụng clickhouse_connect
        """
        # 1️⃣ Kết nối ClickHouse
        client = clickhouse_connect.get_client(
            host='clickhouse',         # nếu Airflow chạy cùng network với ClickHouse => đổi thành 'clickhouse'
            port=8123,
            username='default',
            password='clickhouse_password',
            database='dw'             # nếu bạn có DB 'dw', thêm dòng này
        )

        # 2️⃣ Đọc file parquet
        df = pd.read_parquet(fact_path)
        records = df.to_dict(orient="records")

        # 3️⃣ Chuẩn bị dữ liệu insert
        rows = []
        for r in records:
            date_key = int(r["created_at"].strftime("%Y%m%d"))
            rows.append((
                r["transaction_id"],
                date_key,
                1,  # store_key (tạm cứng)
                r["employee_id"],
                r["customer_id"],
                r["product_id"],
                r["quantity"],
                float(r["discount"]),
                float(r["final_amount"]),
                int(r["point_changed"]),
                r["payment_method"],
                r["created_at"]
            ))

        # 4️⃣ Câu lệnh insert ClickHouse
        insert_query = """
            INSERT INTO dw.fact_sales (
                sales_key, date_key, store_key, employee_key, customer_key, product_key,
                quantity, discount, final_amount, point_changed, payment_method, created_at
            )
            VALUES
        """

        # 5️⃣ Ghi dữ liệu
        client.insert(
            table='dw.fact_sales',
            data=rows,
            column_names=[
                "sales_key", "date_key", "store_key", "employee_key", "customer_key",
                "product_key", "quantity", "discount", "final_amount", "point_changed",
                "payment_method", "created_at"
            ]
        )

        print(f"✅ Loaded {len(rows)} rows into ClickHouse.dw.fact_sales")

        # 6️⃣ Xóa file staging sau khi load xong
        os.remove(fact_path)

    # ========== PIPELINE FLOW ==========
    store_name = "store-1"
    date_str = datetime.now().strftime("%Y_%m_%d")

    local_file = extract_from_minio(store_name, date_str)
    fact_file = transform_to_fact(local_file, store_name)
    load_to_clickhouse(fact_file)


dag = load_pos_to_dw()
