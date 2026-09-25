-- Thanh toán của đúng phiên bản mới nhất của đơn — như stg_sale_line.
SELECT * EXCEPT (_dt, _source_file)
FROM {{ source('bronze', 'bronze_sale_payment') }}
WHERE (sale_id, recorded_at) IN (SELECT sale_id, recorded_at FROM {{ ref('stg_sale') }})
