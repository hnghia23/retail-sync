{{ config(order_by='customer_key') }}
-- SCD1, KHÔNG PII (ràng buộc #10): chỉ customer_id và thuộc tính phân tích. Hạng theo
-- `lifetime_earned`, không theo `balance` (ràng buộc #6) — tính ở trung tâm, dbt chỉ mang theo.
-- Inferred member như dim_store.
-- Bẫy alias của ClickHouse: alias nhìn thấy được trong WHERE, nên
-- `assumeNotNull(customer_id) AS customer_id ... WHERE customer_id IS NOT NULL` kiểm chính alias
-- (không bao giờ NULL) → NULL lọt qua thành UUID toàn số 0. Lọc trong subquery trước.
WITH seen AS (
    SELECT assumeNotNull(customer_id) AS customer_id
    FROM (SELECT customer_id FROM {{ ref('stg_sale') }} WHERE customer_id IS NOT NULL)
    UNION DISTINCT SELECT customer_id FROM {{ ref('stg_point_ledger') }}
)
SELECT
    {{ sk('c.customer_id') }} AS customer_key,
    c.customer_id AS customer_id,
    c.joined_at AS joined_at,
    c.status AS status,
    c.merged_into AS merged_into,
    b.tier AS tier,
    b.lifetime_earned AS lifetime_earned,
    b.balance AS balance,
    b.last_event_at AS last_event_at,
    toUInt8(0) AS is_inferred
FROM {{ ref('stg_customer') }} AS c
LEFT JOIN {{ ref('stg_point_balance') }} AS b ON b.customer_id = c.customer_id
UNION ALL
SELECT {{ sk('customer_id') }}, customer_id, NULL, NULL, NULL, NULL, NULL, NULL, NULL, toUInt8(1)
FROM seen
WHERE customer_id NOT IN (SELECT customer_id FROM {{ ref('stg_customer') }})
