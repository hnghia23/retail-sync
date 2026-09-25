# Airflow 3 + LocalExecutor (ADR-007) + astronomer-cosmos để mỗi dbt model là một task.
#
# Ba môi trường Python trong một image, cố ý tách:
#   1. Airflow + cosmos + packages/pipeline — cài KÈM constraint chính thức của Airflow, nên
#      asyncpg/pyarrow/httpx của pipeline lấy đúng bản Airflow đã kiểm (không kéo lệch Airflow).
#   2. dbt ở /opt/airflow/dbt-venv — venv riêng (data_platform/requirements-dbt.txt). dbt kéo theo
#      protobuf/networkx/... với ràng buộc riêng; trộn vào (1) là vỡ một trong hai. Cosmos gọi
#      dbt qua đường dẫn thực thi.
#   3. Không có gì của edge/central: image này chỉ đọc Postgres trung tâm và ghi lake/ClickHouse.
ARG AIRFLOW_VERSION=3.3.2
FROM apache/airflow:${AIRFLOW_VERSION}-python3.12
ARG AIRFLOW_VERSION

COPY --chown=airflow:root data_platform/requirements.txt /opt/airflow/requirements.txt
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" \
        -r /opt/airflow/requirements.txt \
        --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-3.12.txt"

COPY --chown=airflow:root data_platform/requirements-dbt.txt /opt/airflow/requirements-dbt.txt
RUN python -m venv /opt/airflow/dbt-venv \
    && /opt/airflow/dbt-venv/bin/pip install --no-cache-dir -r /opt/airflow/requirements-dbt.txt

# packages/pipeline — code S4/S5, cùng bản với repo (không phải bản sao viết lại cho Airflow).
COPY --chown=airflow:root packages/pipeline /opt/airflow/lib/pipeline
ENV PYTHONPATH=/opt/airflow/lib

# dbt project và DAG nằm trong image để image tự chạy được; compose mount đè bản ở repo lên
# (read-only) cho vòng lặp phát triển. Cosmos chạy dbt trên bản sao tạm của project, nên
# mount read-only không cản dbt ghi target/.
COPY --chown=airflow:root data_platform/dbt /opt/airflow/dbt
COPY --chown=airflow:root data_platform/airflow/dags /opt/airflow/dags
