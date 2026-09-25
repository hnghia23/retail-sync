{{ config(order_by='product_key') }}
-- SCD1 trong giai đoạn A (docs/06): giá hiện tại. SCD2 theo giá là tính năng C.
-- Inferred member như dim_store.
SELECT
    {{ sk('p.product_id') }} AS product_key,
    p.product_id AS product_id,
    p.sku AS sku,
    p.barcode AS barcode,
    p.name AS name,
    p.category_id AS category_id,
    c.name AS category_name,
    c.parent_category_id AS parent_category_id,
    pc.name AS parent_category_name,
    p.unit_price AS unit_price,
    p.is_sellable AS is_sellable,
    toUInt8(0) AS is_inferred
FROM {{ ref('stg_product') }} AS p
LEFT JOIN {{ ref('stg_category') }} AS c ON c.category_id = p.category_id
LEFT JOIN {{ ref('stg_category') }} AS pc ON pc.category_id = c.parent_category_id
UNION ALL
SELECT
    {{ sk('product_id') }}, product_id, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    toUInt8(1)
FROM (
    -- Một cột, DISTINCT, thẳng từ bronze — không qua `stg_sale_line` (khử trùng toàn bộ lịch sử).
    SELECT DISTINCT product_id FROM {{ source('bronze', 'bronze_sale_line') }}
    WHERE recorded_at < {{ loaded_until() }}
)
WHERE product_id NOT IN (SELECT product_id FROM {{ ref('stg_product') }})
