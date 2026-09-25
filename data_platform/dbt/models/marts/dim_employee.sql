{{ config(order_by='employee_key') }}
-- Không lương, không password_hash (bronze vốn không có). Inferred member như dim_store.
-- Tập "đã thấy trong giao dịch" đọc THẲNG một cột ở bronze (DISTINCT, bộ nhớ cỡ số khóa),
-- không qua `stg_sale`: khử trùng toàn bộ lịch sử mỗi giờ chỉ để lấy một tập khóa là thứ LD-3
-- (2026-09-25) cho thấy không chịu nổi ở T2. Khóa này không đổi giữa các phiên bản của đơn.
WITH seen AS (
    SELECT DISTINCT employee_id FROM {{ source('bronze', 'bronze_sale') }}
    WHERE recorded_at < {{ loaded_until() }}
    UNION DISTINCT SELECT opened_by_employee_id FROM {{ ref('stg_shift') }}
    UNION DISTINCT
    SELECT assumeNotNull(closed_by_employee_id)
    FROM (SELECT closed_by_employee_id FROM {{ ref('stg_shift') }} WHERE closed_by_employee_id IS NOT NULL)
)
SELECT
    {{ sk('employee_id') }} AS employee_key,
    employee_id,
    name,
    role,
    status,
    store_id,
    {{ sk('store_id') }} AS store_key,
    toUInt8(0) AS is_inferred
FROM {{ ref('stg_employee') }}
UNION ALL
SELECT {{ sk('employee_id') }}, employee_id, NULL, NULL, NULL, NULL, toUInt64(0), toUInt8(1)
FROM seen
WHERE employee_id NOT IN (SELECT employee_id FROM {{ ref('stg_employee') }})
