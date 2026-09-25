-- Mỗi đơn trong fact: Σ line_total = subtotal và Σ net_amount = total, CHÍNH XÁC tới đồng.
-- Phân bổ chiết khấu làm tròn sai (thiếu/thừa vài đồng) chỉ lộ ra ở đây.
SELECT f.sale_id, f.gross, s.subtotal, f.net, s.total
FROM (
    SELECT sale_id, sum(line_total) AS gross, sum(net_amount) AS net
    FROM {{ ref('fact_sale_line') }}
    GROUP BY sale_id
) AS f
INNER JOIN {{ ref('stg_sale') }} AS s ON s.sale_id = f.sale_id
WHERE f.gross != s.subtotal OR f.net != s.total
