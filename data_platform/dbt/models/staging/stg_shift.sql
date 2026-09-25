SELECT * EXCEPT (_dt, _source_file)
FROM {{ source('bronze', 'bronze_shift') }}
WHERE recorded_at < {{ loaded_until() }}
ORDER BY shift_id, recorded_at DESC
LIMIT 1 BY shift_id
