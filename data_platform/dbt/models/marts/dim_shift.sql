{{ config(order_by='shift_key') }}
-- Ca chỉ lên trung tâm KHI ĐÓNG (`ShiftClosed`, không có sự kiện mở ca). DAG chạy giữa ngày thì
-- mọi đơn của ca đang mở trỏ tới một ca trung tâm chưa biết. Phát hiện 2026-09-24: bộ giả lập
-- `virtual` 20 cửa hàng, DAG chạy lúc cửa hàng đang bán → test relationships đỏ, chặn luồng.
--
-- Nên dim có dòng inferred cho ca thấy trong đơn mà chưa có ở `stg_shift`: cửa hàng + ngày kinh
-- doanh lấy từ đơn, còn lại NULL, `is_closed = 0`. Đây là trạng thái BÌNH THƯỜNG (ca đang mở),
-- không phải lỗi dữ liệu, nên không có test cảnh báo như các dim khác. Ca đóng → lượt dựng sau
-- có dòng thật thay thế (dim dựng lại trọn mỗi lượt).
SELECT
    {{ sk('shift_id') }} AS shift_key,
    shift_id,
    {{ sk('store_id') }} AS store_key,
    store_id,
    business_date,
    {{ date_key('business_date') }} AS date_key,
    {{ sk('opened_by_employee_id') }} AS opened_by_employee_key,
    {{ sk('closed_by_employee_id') }} AS closed_by_employee_key,
    opened_at,
    closed_at,
    toUInt8(closed_at IS NOT NULL) AS is_closed,
    opening_cash,
    expected_cash,
    counted_cash,
    variance,
    toUInt8(0) AS is_inferred
FROM {{ ref('stg_shift') }}
UNION ALL
SELECT
    {{ sk('shift_id') }},
    shift_id,
    {{ sk('store_id') }},
    store_id,
    business_date,
    {{ date_key('business_date') }},
    toUInt64(0),
    toUInt64(0),
    NULL,
    NULL,
    toUInt8(0),
    NULL,
    NULL,
    NULL,
    NULL,
    toUInt8(1)
FROM (
    SELECT assumeNotNull(shift_id) AS shift_id, any(store_id) AS store_id,
           min(business_date) AS business_date
    FROM (SELECT shift_id, store_id, business_date FROM {{ ref('stg_sale') }} WHERE shift_id IS NOT NULL)
    GROUP BY shift_id
)
WHERE shift_id NOT IN (SELECT shift_id FROM {{ ref('stg_shift') }})
