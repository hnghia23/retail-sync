-- Mỗi đơn trong fact: Σ line_total = subtotal và Σ net_amount = total, CHÍNH XÁC tới đồng.
-- Phân bổ chiết khấu làm tròn sai (thiếu/thừa vài đồng) chỉ lộ ra ở đây.
SELECT f.sale_id, f.gross, s.subtotal, f.net, s.total
FROM (
    SELECT sale_id, sum(line_total) AS gross, sum(net_amount) AS net
    FROM {{ ref('fact_sale_line') }}
    WHERE occurred_at >= now64(6) - toIntervalDay({{ var('test_window_days', 35) }})
    GROUP BY sale_id
) AS f
INNER JOIN (
    SELECT sale_id, subtotal, total FROM {{ source('bronze', 'bronze_sale') }}
    WHERE recorded_at < {{ loaded_until() }}
      AND occurred_at >= now64(6) - toIntervalDay({{ var('test_window_days', 35) }})
    ORDER BY sale_id, recorded_at DESC
    LIMIT 1 BY sale_id
) AS s ON s.sale_id = f.sale_id
WHERE f.gross != s.subtotal OR f.net != s.total
