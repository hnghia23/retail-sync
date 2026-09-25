{{ config(order_by='store_key') }}
-- Inferred member (Kimball): cửa hàng có trong giao dịch mà chưa có trong master data vẫn có
-- một dòng, `is_inferred = 1`, thuộc tính NULL. Trung tâm cố ý nhận đơn không cần khóa ngoại
-- tới master data (cửa hàng tự chủ, master data có thể lệch), nên fact PHẢI chịu được điều đó —
-- test cảnh báo (warn) chứ không chặn luồng.
WITH seen AS (
    SELECT store_id FROM {{ ref('stg_sale') }}
    UNION DISTINCT SELECT store_id FROM {{ ref('stg_shift') }}
    UNION DISTINCT SELECT store_id FROM {{ ref('stg_point_ledger') }}
)
SELECT
    {{ sk('s.store_id') }} AS store_key,
    s.store_id AS store_id,
    s.name AS name,
    s.city AS city,
    s.region_id AS region_id,
    r.name AS region_name,
    s.status AS status,
    s.opened_at AS opened_at,
    s.closed_at AS closed_at,
    toUInt8(0) AS is_inferred
FROM {{ ref('stg_store') }} AS s
LEFT JOIN {{ ref('stg_region') }} AS r ON r.region_id = s.region_id
UNION ALL
SELECT
    {{ sk('store_id') }}, store_id, NULL, NULL, NULL, NULL, NULL, NULL, NULL, toUInt8(1)
FROM seen
WHERE store_id NOT IN (SELECT store_id FROM {{ ref('stg_store') }})
