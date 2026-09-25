-- Silver: phiên bản MỚI NHẤT của mỗi đơn (một đơn bị UPDATE có nhiều phiên bản ở bronze,
-- docs/17 §4 bẫy 3), chỉ trong phần bronze đã nạp liền mạch (`loaded_until`).
SELECT * EXCEPT (_dt, _source_file)
FROM {{ source('bronze', 'bronze_sale') }}
WHERE recorded_at < {{ loaded_until() }}
ORDER BY sale_id, recorded_at DESC
LIMIT 1 BY sale_id
