-- Watermark của bảng này là `updated_at` (docs/17 §2).
SELECT * EXCEPT (_dt, _source_file)
FROM {{ source('bronze', 'bronze_point_balance') }}
WHERE updated_at < {{ loaded_until() }}
ORDER BY customer_id, updated_at DESC
LIMIT 1 BY customer_id
