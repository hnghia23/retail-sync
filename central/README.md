# central

This folder contains the central infrastructure prototype for the retail-sync project.
It includes a local docker-compose for MinIO (data lake), Cassandra (loyalty central DB),
and ClickHouse (data warehouse prototype), a sample Cassandra schema and a minimal
ingestion script to read objects from MinIO.

Getting started (Windows PowerShell):

1. Start services:

   docker-compose -f central/docker-compose.yml up -d

2. Apply Cassandra schema:

   # from repository root (or adjust path)
   docker exec -i $(docker ps -qf "name=cassandra") cqlsh -f /schema.cql

3. Upload sample files to MinIO using the web UI (http://localhost:9001) or `mc`.

Files added:
- `docker-compose.yml` - local dev stack (MinIO, Cassandra, ClickHouse)
- `cassandra/schema.cql` - keyspace and tables for loyalty
- `etl/ingest_minio.py` - minimal example showing how to read files from MinIO
- `.gitignore` - ignores local data folders

Next steps:
- Wire an Airbyte or Debezium + Kafka pipeline if you need CDC streaming from store DBs.
- Implement ETL jobs (dbt/Airflow) to load cleaned data into ClickHouse and update Cassandra for loyalty adjustments.
