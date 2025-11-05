from airflow import DAG 
from airflow.providers.mysql.hooks.mysql import MySqlHook
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.decorators import dag, task
from datetime import datetime, timedelta

import os 
import pandas as pd 
import json 
import logging


with open('config/pos_config.json', 'r') as f:
    pos_configs = json.load(f)

TABLES = ["transactions", "transaction_item"]

today_date = datetime.now().strftime("%Y_%m_%d")

@task
def extract_transactions(pos_config):
    mysql_hook = MySqlHook(mysql_conn_id=pos_config['mysql_conn_id_airflow'])
    query = """
        SELECT * 
        FROM transactions 
        WHERE created_at <= NOW() - INTERVAL 7 DAY;
    """
    df_trans = mysql_hook.get_pandas_df(query)

    # Lưu tạm
    trans_path = f"/tmp/{pos_config['store_name']}_transactions.parquet"
    df_trans.to_parquet(trans_path, index=False)

    # Upload lên MinIO
    s3_hook = S3Hook(aws_conn_id='minio_conn_id')
    bucket = "pos"
    s3_key = f"staging/{pos_config['store_name']}/{today_date}/transactions.parquet"
    s3_hook.load_file(filename=trans_path, key=s3_key, bucket_name=bucket, replace=True)
    os.remove(trans_path)

    # Trả danh sách transaction_id để task kế tiếp dùng
    return df_trans["transaction_id"].tolist()


@task
def extract_transaction_items(transaction_ids, pos_config):
    mysql_hook = MySqlHook(mysql_conn_id=pos_config['mysql_conn_id_airflow'])

    # Truy vấn theo transaction_id
    ids_str = ",".join(map(str, transaction_ids)) or "NULL"
    query = f"SELECT * FROM transaction_item WHERE transaction_id IN ({ids_str});"
    df_items = mysql_hook.get_pandas_df(query)

    # Ghi ra file
    tmp_path = f"/tmp/{pos_config['store_name']}_transaction_item.parquet"
    df_items.to_parquet(tmp_path, index=False)

    s3_hook = S3Hook(aws_conn_id='minio_conn_id')
    bucket = "pos"
    s3_key = f"staging/{pos_config['store_name']}/{today_date}/transaction_item.parquet"
    s3_hook.load_file(filename=tmp_path, key=s3_key, bucket_name=bucket, replace=True)
    os.remove(tmp_path)

    return s3_key

@task 
def load_to_lake(pos_config, **kwargs):
    s3_hook = S3Hook(aws_conn_id='minio_conn_id')
    bucket_name = "pos"

    store_name = pos_config['store_name']
    staging_dir = f"/tmp/{store_name}/staging"
    os.makedirs(staging_dir, exist_ok=True)

    # Đường dẫn thực tế giống với extract_table
    trans_key = f"staging/{store_name}/{today_date}/transactions.parquet"
    trans_item_key = f"staging/{store_name}/{today_date}/transaction_item.parquet"

    trans_path = os.path.join(staging_dir, "transactions.parquet")
    trans_item_path = os.path.join(staging_dir, "transaction_item.parquet")

    # Kiểm tra object tồn tại trước khi tải
    s3_client = s3_hook.get_conn()
    for key in [trans_key, trans_item_key]:
        try:
            s3_client.head_object(Bucket=bucket_name, Key=key)
        except Exception:
            raise FileNotFoundError(f"❌ Object not found in MinIO: s3://{bucket_name}/{key}")

    # Download từ MinIO
    s3_hook.get_key(trans_key, bucket_name=bucket_name).download_file(trans_path)
    s3_hook.get_key(trans_item_key, bucket_name=bucket_name).download_file(trans_item_path)

    # --- Các bước merge ---
    df_trans = pd.read_parquet(trans_path)
    df_item = pd.read_parquet(trans_item_path)

    df_item["product_rank"] = df_item.groupby("transaction_id").cumcount() + 1
    df_pivot = df_item.pivot_table(
        index="transaction_id",
        values=["product_id", "quantity"],
        columns="product_rank",
        aggfunc="first"
    )
    df_pivot.columns = [f"{col[0]}_{col[1]}" for col in df_pivot.columns]
    df_pivot = df_pivot.reset_index()

    df_merged = pd.merge(df_trans, df_pivot, on="transaction_id", how="left")

    # Upload kết quả
    merged_path = "/tmp/order_merged.parquet"
    df_merged.to_parquet(merged_path, index=False)

    output_key = f"raw/{store_name}/{today_date}.parquet"
    s3_hook.load_file(filename=merged_path, key=output_key, bucket_name=bucket_name, replace=True)
    print(f"✅ Uploaded merged file to s3://{bucket_name}/{output_key}")

    os.remove(trans_path)
    os.remove(trans_item_path)
    os.remove(merged_path)



default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2025, 1, 1),
    'retries': 3,
    'retry_delay': timedelta(minutes=1),
}


# --- DAG ---
with DAG(
    dag_id="ingest_to_lake",
    default_args=default_args,
    schedule_interval="@daily",
    catchup=False,
    tags=["pos", "data-lake"],
) as dag:

    all_extract_tasks = []

    for cfg in pos_configs:
        trans_task = extract_transactions.override(task_id=f"extract_{cfg['store_name']}_transactions")(cfg)
        item_task = extract_transaction_items.override(task_id=f"extract_{cfg['store_name']}_items")(trans_task, cfg)
        load_task = load_to_lake.override(task_id=f"load_{cfg['store_name']}")(cfg)

        trans_task >> item_task >> load_task
