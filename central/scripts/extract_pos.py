import pandas as pd
from datetime import datetime
import logging

# Hàm extract bảng từ MySQL
def extract_table(conn, table_name):
    cursor = conn.cursor()

    try:
        cursor.execute(f"SELECT * FROM {table_name}")
        rows = cursor.fetchall()
        cols = [desc[0] for desc in cursor.description]
        df = pd.DataFrame(rows, columns=cols)
        logging.info(f"Loaded {table_name}: {len(df)} rows")
        return df
    except Exception as e:
        logging.error(f"Error extracting {table_name}: {e}")
        return pd.DataFrame()

# Hàm ghi bảng ra file staging
def save_parquet_file(df, file_path):
    if not df.empty:
        try:
            date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = f"{file_path}_{date_str}.parquet"
            df.to_parquet(output_path, index=False)
            logging.info(f"Saved file at {file_path}")

            return output_path
        
        except Exception as e:
            logging.error(f"Error saving file {file_path}: {e}")
    else:
        logging.warning("Không có dữ liệu để export.")
        return None


