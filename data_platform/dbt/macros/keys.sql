{#-
  Khóa thay thế TẤT ĐỊNH: cityHash64 của khóa nghiệp vụ dạng chuỗi.

  Không dùng số tự tăng: dim và fact dựng độc lập, dựng lại từ bronze (cổng A) phải ra đúng
  khóa cũ, và fact không phải tra dim để lấy khóa. Một kiểu duy nhất (UInt64) cho mọi khóa —
  lỗi v1 là `product_key String` đối chiếu với `UInt32` (docs/05 §7).

  0 = "không có" (khách vãng lai, đơn không thuộc ca nào). Test `relationships` bỏ qua 0.
-#}
{% macro sk(expr) -%}
if(isNull({{ expr }}), toUInt64(0), cityHash64(toString(assumeNotNull({{ expr }}))))
{%- endmacro %}

{% macro date_key(expr) -%}
toUInt32(toYYYYMMDD({{ expr }}))
{%- endmacro %}
