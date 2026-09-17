# Airflow 3 + LocalExecutor (ADR-007) + astronomer-cosmos để mỗi dbt model là một task.
FROM apache/airflow:3.0.3-python3.12

USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*
USER airflow

COPY --chown=airflow:root data_platform/requirements.txt /opt/airflow/requirements.txt
RUN pip install --no-cache-dir -r /opt/airflow/requirements.txt

COPY --chown=airflow:root data_platform/dbt /opt/airflow/dbt
