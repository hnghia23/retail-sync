-- Không PII (ràng buộc #10): bronze_customer vốn không mang SĐT/tên dưới dạng nào.
SELECT * EXCEPT (_dt, _source_file)
FROM {{ source('bronze', 'bronze_customer') }}
WHERE recorded_at < {{ loaded_until() }}
ORDER BY customer_id, recorded_at DESC
LIMIT 1 BY customer_id
