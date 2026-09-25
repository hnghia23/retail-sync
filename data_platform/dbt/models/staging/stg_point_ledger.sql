-- Sổ cái append-only (ADR-002): mỗi sự kiện một dòng, không có phiên bản. `LIMIT 1 BY` chỉ để
-- staging luôn một dòng/khóa — bronze nhân đôi thì test `assert_bronze_has_no_duplicate_versions`
-- đỏ, không bị che ở đây.
SELECT * EXCEPT (_dt, _source_file)
FROM {{ source('bronze', 'bronze_point_ledger') }}
WHERE recorded_at < {{ loaded_until() }}
ORDER BY event_id, recorded_at DESC
LIMIT 1 BY event_id
