{#-
  Mép "đã nạp tới" và bẫy 5 (docs/17 §4).

  ## loaded_until()

  Mọi bảng incremental đã nạp LIỀN MẠCH tới mốc này (nhật ký nạp, file nạp theo thứ tự thời
  gian). Staging chỉ nhìn phần dưới mốc, nên mọi fact thấy CÙNG một lát cắt. Không có mốc thì
  nạp chết giữa chừng (đã có `sale` + `sale_line` của cửa sổ W, chưa có `sale_payment`) cho ra
  `fact_sale_line` có đơn mà `fact_payment` chưa có — test cổng A "Σ thanh toán = Σ bán theo
  ngày" đỏ GIẢ và chặn cả luồng. (Không mất vĩnh viễn: dòng nạp muộn vẫn có `recorded_at` lớn
  hơn mốc của fact nên lần sau được tính — chính test tích hợp chỉ ra điều này.)

  Đòi ĐỦ bảng: bảng vắng khỏi nhật ký → mốc = 1970 (không thấy gì). Bảng rỗng vẫn có một file
  mốc (`extract_incremental`), nên "vắng" chỉ có nghĩa là chưa nạp.

  ## pipeline_cutoff()

  Cùng giá trị, nhưng tính MỘT lần lúc dbt chạy model và nhúng vào SQL dưới dạng hằng. Fact
  dùng mép hai lần (tập tháng bị ảnh hưởng + dữ liệu); nếu mỗi chỗ tự tính thì một lần nạp chen
  vào giữa sẽ cho hai mép khác nhau, và dòng nằm giữa hai mép thuộc tháng "không bị ảnh hưởng"
  sẽ bị bỏ qua VĨNH VIỄN (lần sau `_recorded_at` lớn nhất đã vượt qua chúng).

  ## affected_month_list(table, version_col, cutoff) — macros/incremental.sql

  Bẫy 5: tập tháng cần thay = tháng của `occurred_at` của các dòng MỚI (recorded_at lớn hơn mốc
  lớn nhất đã có trong fact). Cửa hàng offline vắt qua cuối tháng → tháng trước nằm trong tập,
  và được tính lại TRỌN tháng (`insert_overwrite` thay nguyên phân vùng). Không hardcode
  "tháng này". Bản đầu (`affected_months`) đọc view staging khử trùng toàn bộ lịch sử — thay ở
  LD-3 bằng bản đọc thẳng bronze có cắt phân vùng.
-#}

{% macro incremental_tables() -%}
{{ return(['sale', 'sale_line', 'sale_payment', 'point_ledger', 'shift', 'customer', 'point_balance']) }}
{%- endmacro %}

{% macro loaded_until() -%}
{%- set tables = incremental_tables() -%}
(
    SELECT if(count() = {{ tables | length }}, assumeNotNull(min(m)), toDateTime64(0, 6, 'UTC'))
    FROM (
        SELECT table_name, max(window_end) AS m
        FROM {{ source('bronze', 'bronze_load_log') }}
        WHERE table_name IN ('{{ tables | join("', '") }}')
        GROUP BY table_name
    )
)
{%- endmacro %}

{% macro pipeline_cutoff() -%}
{%- if execute -%}
    {%- set value = run_query("SELECT toString(" ~ loaded_until() ~ ")").columns[0].values()[0] -%}
    toDateTime64('{{ value }}', 6, 'UTC')
{%- else -%}
    toDateTime64(0, 6, 'UTC')
{%- endif -%}
{%- endmacro %}
