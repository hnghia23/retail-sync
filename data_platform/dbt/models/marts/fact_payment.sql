{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    engine='MergeTree()',
    partition_by='toYYYYMM(occurred_at)',
    order_by='(store_key, date_key, sale_id, seq)',
) }}
{#-
  BẢNG RIÊNG cho thanh toán (ràng buộc #5): một đơn có N dòng hàng VÀ M lần thanh toán — gộp
  chung thì join thành tích Descartes, nhân doanh thu N×M lần. Tăng dần như fact_sale_line.
-#}
{%- set cutoff = pipeline_cutoff() %}

WITH sales AS (
    SELECT *
    FROM {{ ref('stg_sale') }}
    WHERE recorded_at < {{ cutoff }}
    {%- if is_incremental() %}
      AND toYYYYMM(occurred_at) IN ({{ affected_months(ref('stg_sale'), cutoff) }})
    {%- endif %}
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
FROM {{ ref('stg_sale_payment') }} AS p
INNER JOIN sales AS s ON s.sale_id = p.sale_id AND s.recorded_at = p.recorded_at
