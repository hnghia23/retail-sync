{{ config(order_by='date_key') }}
-- Lịch sinh sẵn 2020–2035. `is_holiday` chưa có: lịch nghỉ lễ Việt Nam (âm lịch) cần seed từ
-- nguồn chính thức — đoán ở đây là dữ liệu sai trông như đúng.
SELECT
    {{ date_key('d') }} AS date_key,
    d AS full_date,
    toDayOfMonth(d) AS day,
    toMonth(d) AS month,
    toQuarter(d) AS quarter,
    toYear(d) AS year,
    toISOWeek(d) AS iso_week,
    toDayOfWeek(d) AS weekday,
    ['Thứ Hai', 'Thứ Ba', 'Thứ Tư', 'Thứ Năm', 'Thứ Sáu', 'Thứ Bảy', 'Chủ Nhật'][toDayOfWeek(d)]
        AS weekday_name,
    toUInt8(toDayOfWeek(d) >= 6) AS is_weekend
FROM (
    SELECT toDate32('2020-01-01') + number AS d
    FROM numbers(toUInt64(dateDiff('day', toDate32('2020-01-01'), toDate32('2035-12-31')) + 1))
)
