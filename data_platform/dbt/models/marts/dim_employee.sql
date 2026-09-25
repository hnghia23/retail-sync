{{ config(order_by='employee_key') }}
-- Không lương, không password_hash (bronze vốn không có). Inferred member như dim_store.
WITH seen AS (
    SELECT employee_id FROM {{ ref('stg_sale') }}
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
