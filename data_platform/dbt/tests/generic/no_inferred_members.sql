{#- Dòng inferred = giao dịch trỏ tới master data chưa có. Chất lượng dữ liệu, không phải mất
    dữ liệu: khai `severity: warn` để báo mà không chặn luồng. -#}
{% test no_inferred_members(model) %}
SELECT * FROM {{ model }} WHERE is_inferred = 1
{% endtest %}
