-- AT-07 ở tầng bronze: cùng một phiên bản (khóa + recorded_at) xuất hiện hai lần = khử trùng
-- khi nạp hỏng (bẫy 4). Staging `LIMIT 1 BY` sẽ che lỗi này, nên phải kiểm ngay trên bronze.
{%- set checks = [
    ('bronze_sale', 'sale_id, recorded_at'),
    ('bronze_sale_line', 'sale_id, line_no, recorded_at'),
    ('bronze_sale_payment', 'sale_id, seq, recorded_at'),
    ('bronze_point_ledger', 'event_id'),
    ('bronze_shift', 'shift_id, recorded_at'),
    ('bronze_customer', 'customer_id, recorded_at'),
    ('bronze_point_balance', 'customer_id, updated_at'),
] %}
{%- for table, key in checks %}
SELECT '{{ table }}' AS tbl, count() AS n
FROM {{ source('bronze', table) }}
GROUP BY {{ key }}
HAVING n > 1
{{ 'UNION ALL' if not loop.last }}
{%- endfor %}
