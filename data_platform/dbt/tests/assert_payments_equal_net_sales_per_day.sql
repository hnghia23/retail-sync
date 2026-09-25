-- Cổng A (docs/06): theo (cửa hàng, ngày), Σ fact_payment.amount = Σ fact_sale_line.net_amount.
-- Lệch = mô hình chiều sai (nhân N×M, ràng buộc #5) hoặc phân bổ chiết khấu sai.
-- (Cổng A ghi `line_total`; đúng ra là `net_amount` = line_total − chiết khấu phân bổ, vì
-- thanh toán bằng `total` SAU chiết khấu.)
SELECT store_key, date_key, paid, sold
FROM (
    SELECT store_key, date_key, sum(amount) AS paid
    FROM {{ ref('fact_payment') }}
    GROUP BY store_key, date_key
) AS p
FULL OUTER JOIN (
    SELECT store_key, date_key, sum(net_amount) AS sold
    FROM {{ ref('fact_sale_line') }}
    GROUP BY store_key, date_key
) AS s USING (store_key, date_key)
WHERE coalesce(paid, 0) != coalesce(sold, 0)
