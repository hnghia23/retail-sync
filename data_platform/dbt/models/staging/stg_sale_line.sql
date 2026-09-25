-- Dòng hàng của ĐÚNG phiên bản mới nhất của đơn: dòng con mang `recorded_at` của đơn cha
-- (trích cùng cửa sổ), nên khớp theo cặp (sale_id, recorded_at).
SELECT * EXCEPT (_dt, _source_file)
FROM {{ source('bronze', 'bronze_sale_line') }}
WHERE (sale_id, recorded_at) IN (SELECT sale_id, recorded_at FROM {{ ref('stg_sale') }})
