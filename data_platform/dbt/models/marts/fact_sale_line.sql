{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    engine='MergeTree()',
    partition_by='toYYYYMM(occurred_at)',
    order_by='(store_key, date_key, sale_id, line_no)',
) }}
{#-
  Mức chi tiết nhất: MỘT DÒNG SẢN PHẨM (docs/05 §7).

  Tăng dần theo bẫy 5: chỉ dựng lại những tháng có dòng mới (`affected_months`), và dựng lại
  TRỌN tháng — `insert_overwrite` thay nguyên phân vùng bằng kết quả. MergeTree thường, không
  ReplacingMergeTree: phân vùng được thay trọn gói nên không cần lưới an toàn, và lưới đó sẽ
  CHE lỗi nhân đôi mà bộ đối soát L4 phải thấy.

  Chiết khấu cấp đơn (hạng + khuyến mãi) được phân bổ xuống dòng theo tỉ lệ `line_total`, phần
  dư do làm tròn dồn vào dòng cuối, để Σ `net_amount` của một đơn = `total` CHÍNH XÁC tới đồng
  — và bằng Σ `fact_payment.amount` (cổng A).
-#}
{%- set cutoff = pipeline_cutoff() %}

WITH sales AS (
    SELECT *
    FROM {{ ref('stg_sale') }}
    WHERE recorded_at < {{ cutoff }}
    {%- if is_incremental() %}
      AND toYYYYMM(occurred_at) IN ({{ affected_months(ref('stg_sale'), cutoff) }})
    {%- endif %}
),

lines AS (
    SELECT
        l.sale_id AS sale_id,
        l.line_no AS line_no,
        l.product_id AS product_id,
        l.quantity AS quantity,
        l.unit_price AS unit_price,
        l.line_total AS line_total,
        l.original_sale_id AS original_sale_id,
        s.store_id AS store_id,
        s.shift_id AS shift_id,
        s.business_date AS business_date,
        s.employee_id AS employee_id,
        s.customer_id AS customer_id,
        s.subtotal AS subtotal,
        s.discount_tier + s.discount_promo AS discount_total,
        s.status AS status,
        s.occurred_at AS occurred_at,
        s.recorded_at AS recorded_at,
        toInt64(if(
            s.subtotal = 0, 0,
            intDiv(toInt128(s.discount_tier + s.discount_promo) * l.line_total, s.subtotal)
        )) AS base_discount
    FROM {{ ref('stg_sale_line') }} AS l
    INNER JOIN sales AS s ON s.sale_id = l.sale_id AND s.recorded_at = l.recorded_at
),

allocated AS (
    SELECT
        *,
        sum(base_discount) OVER (PARTITION BY sale_id) AS base_sum,
        row_number() OVER (PARTITION BY sale_id ORDER BY line_no DESC) AS rn_desc
    FROM lines
)

SELECT
    {{ date_key('business_date') }} AS date_key,
    business_date,
    {{ sk('store_id') }} AS store_key,
    {{ sk('shift_id') }} AS shift_key,
    {{ sk('employee_id') }} AS employee_key,
    {{ sk('customer_id') }} AS customer_key,
    {{ sk('product_id') }} AS product_key,
    sale_id,
    line_no,
    quantity,
    unit_price,
    line_total,
    toInt64(base_discount + if(rn_desc = 1, discount_total - base_sum, 0)) AS discount_allocated,
    toInt64(line_total - discount_allocated) AS net_amount,
    toUInt8(original_sale_id IS NOT NULL) AS is_return,
    status AS sale_status,
    occurred_at,
    recorded_at AS _recorded_at
FROM allocated
