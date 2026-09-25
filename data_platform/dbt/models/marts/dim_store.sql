{{ config(order_by='store_key') }}
-- Inferred member (Kimball): cửa hàng có trong giao dịch mà chưa có trong master data vẫn có
-- một dòng, `is_inferred = 1`, thuộc tính NULL. Trung tâm cố ý nhận đơn không cần khóa ngoại
-- tới master data (cửa hàng tự chủ, master data có thể lệch), nên fact PHẢI chịu được điều đó —
-- test cảnh báo (warn) chứ không chặn luồng.
-- Tập "đã thấy trong giao dịch" đọc THẲNG một cột ở bronze (DISTINCT, bộ nhớ cỡ số khóa),
-- không qua `stg_sale`: khử trùng toàn bộ lịch sử mỗi giờ chỉ để lấy một tập khóa là thứ LD-3
-- (2026-09-25) cho thấy không chịu nổi ở T2. Khóa này không đổi giữa các phiên bản của đơn.
WITH seen AS (
    SELECT DISTINCT store_id FROM {{ source('bronze', 'bronze_sale') }}
    WHERE recorded_at < {{ loaded_until() }}
    UNION DISTINCT SELECT store_id FROM {{ ref('stg_shift') }}
    UNION DISTINCT
    SELECT DISTINCT store_id FROM {{ source('bronze', 'bronze_point_ledger') }}
    WHERE recorded_at < {{ loaded_until() }}
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
