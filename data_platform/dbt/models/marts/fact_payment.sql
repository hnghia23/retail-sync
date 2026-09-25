{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    engine='MergeTree()',
    partition_by='toYYYYMM(occurred_at)',
    order_by='(store_key, date_key, sale_id, seq)',
) }}
{#-
  BẢNG RIÊNG cho thanh toán (ràng buộc #5): một đơn có N dòng hàng VÀ M lần thanh toán — gộp
  chung thì join thành tích Descartes, nhân doanh thu N×M lần. Tăng dần như fact_sale_line
  (đọc thẳng bronze, O(tháng bị ảnh hưởng) — macros/incremental.sql).
-#}
{%- set cutoff = pipeline_cutoff() %}
{%- set months = affected_month_list('sale', 'recorded_at', cutoff) %}

WITH sales AS (
    {{ latest_sales(cutoff, months) }}
)

SELECT
    {{ date_key('s.business_date') }} AS date_key,
    s.business_date AS business_date,
    {{ sk('s.store_id') }} AS store_key,
    {{ sk('s.shift_id') }} AS shift_key,
    p.sale_id AS sale_id,
    p.seq AS seq,
    p.method AS method,
    p.amount AS amount,
    s.status AS sale_status,
    s.occurred_at AS occurred_at,
    s.recorded_at AS _recorded_at
FROM (
    SELECT * EXCEPT (_dt, _source_file)
    FROM {{ source('bronze', 'bronze_sale_payment') }}
    WHERE recorded_at < {{ cutoff }}{{ child_dt_filter('sale_payment', months) }}
      AND (sale_id, recorded_at) IN (SELECT sale_id, recorded_at FROM sales)
) AS p
INNER JOIN sales AS s ON s.sale_id = p.sale_id AND s.recorded_at = p.recorded_at
