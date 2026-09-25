{{ config(order_by='customer_key') }}
-- SCD1, KHÔNG PII (ràng buộc #10): chỉ customer_id và thuộc tính phân tích. Hạng theo
-- `lifetime_earned`, không theo `balance` (ràng buộc #6) — tính ở trung tâm, dbt chỉ mang theo.
-- Inferred member như dim_store.
-- Bẫy alias của ClickHouse: alias nhìn thấy được trong WHERE, nên
-- `assumeNotNull(customer_id) AS customer_id ... WHERE customer_id IS NOT NULL` kiểm chính alias
-- (không bao giờ NULL) → NULL lọt qua thành UUID toàn số 0. Lọc trong subquery trước.
-- Tập "đã thấy trong giao dịch" đọc THẲNG một cột ở bronze (DISTINCT, bộ nhớ cỡ số khóa),
-- không qua `stg_sale`: khử trùng toàn bộ lịch sử mỗi giờ chỉ để lấy một tập khóa là thứ LD-3
-- (2026-09-25) cho thấy không chịu nổi ở T2. Khóa này không đổi giữa các phiên bản của đơn.
WITH seen AS (
    SELECT assumeNotNull(customer_id) AS customer_id
    FROM (
        SELECT DISTINCT customer_id FROM {{ source('bronze', 'bronze_sale') }}
        WHERE customer_id IS NOT NULL AND recorded_at < {{ loaded_until() }}
    )
    UNION DISTINCT
    SELECT DISTINCT customer_id FROM {{ source('bronze', 'bronze_point_ledger') }}
    WHERE recorded_at < {{ loaded_until() }}
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
