-- Master data chụp nguyên bảng mỗi ngày: lấy bản chụp MỚI NHẤT.
SELECT * EXCEPT (_dt, _source_file)
FROM {{ source('bronze', 'bronze_category') }}
WHERE _dt = (SELECT max(_dt) FROM {{ source('bronze', 'bronze_category') }})
ORDER BY category_id
LIMIT 1 BY category_id
