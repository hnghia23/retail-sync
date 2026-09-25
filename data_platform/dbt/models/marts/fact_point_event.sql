{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    engine='MergeTree()',
    partition_by='toYYYYMM(occurred_at)',
    order_by='(customer_key, date_key, event_id)',
) }}
{#-
  Ánh xạ gần 1-1 từ sổ cái điểm (ADR-002). Sổ cái không có business_date: date_key theo giờ
  cửa hàng (`var('store_timezone')`). Tăng dần như fact_sale_line (O(tháng bị ảnh hưởng)).
-#}
{%- set cutoff = pipeline_cutoff() %}
{%- set months = affected_month_list('point_ledger', 'recorded_at', cutoff) %}

SELECT
    {{ date_key("toTimeZone(occurred_at, '" ~ var('store_timezone') ~ "')") }} AS date_key,
    {{ sk('store_id') }} AS store_key,
    {{ sk('customer_id') }} AS customer_key,
    event_id,
    sale_id,
    delta,
    reason,
    occurred_at,
    recorded_at AS _recorded_at
FROM (
    -- Sổ cái bất biến (ADR-002): mỗi `event_id` một dòng; LIMIT BY chỉ chặn bản nạp trùng.
    SELECT * EXCEPT (_dt, _source_file)
    FROM {{ source('bronze', 'bronze_point_ledger') }}
    WHERE recorded_at < {{ cutoff }}{{ month_filter('point_ledger', months) }}
    ORDER BY event_id, recorded_at DESC
    LIMIT 1 BY event_id
)
