-- Master data chụp nguyên bảng mỗi ngày: lấy bản chụp MỚI NHẤT.
SELECT * EXCEPT (_dt, _source_file)
FROM {{ source('bronze', 'bronze_employee') }}
WHERE _dt = (SELECT max(_dt) FROM {{ source('bronze', 'bronze_employee') }})
ORDER BY employee_id
LIMIT 1 BY employee_id
