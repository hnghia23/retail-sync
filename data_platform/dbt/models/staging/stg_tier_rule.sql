-- Master data chụp nguyên bảng mỗi ngày: lấy bản chụp MỚI NHẤT.
SELECT * EXCEPT (_dt, _source_file)
FROM {{ source('bronze', 'bronze_tier_rule') }}
WHERE _dt = (SELECT max(_dt) FROM {{ source('bronze', 'bronze_tier_rule') }})
ORDER BY tier
LIMIT 1 BY tier
