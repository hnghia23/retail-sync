{#-
  Dựng fact TĂNG DẦN với chi phí O(tháng bị ảnh hưởng), không phải O(toàn bộ lịch sử).

  ## Vì sao (LD-3, 2026-09-25)

  Bản đầu đọc view staging (`stg_sale`: `ORDER BY sale_id, recorded_at DESC LIMIT 1 BY sale_id`
  trên TOÀN BỘ bronze) rồi mới lọc tháng. ClickHouse không đẩy điều kiện tháng xuống dưới
  `LIMIT BY` được (đổi nghĩa), nên MỖI lượt DAG (mỗi giờ) sắp xếp toàn bộ lịch sử bán hàng —
  ở T2 là 78 triệu đơn, và cả `affected_months()` lẫn 5 dim cùng làm lại việc đó.

  ## Đẩy điều kiện xuống TRƯỚC khi khử trùng — đúng vì hai bất biến

  1. `occurred_at` của một đơn không đổi giữa các phiên bản (UPDATE ở trung tâm chỉ đổi trạng
     thái/cột phụ; trả hàng/hủy là ĐƠN MỚI). Lọc `toYYYYMM(occurred_at) IN (...)` trước
     `LIMIT 1 BY sale_id` cho đúng tập phiên bản như lọc sau.
  2. `recorded_at` ≥ `occurred_at` − 1 ngày: trung tâm ghi nhận SAU khi cửa hàng bán, trừ khi
     đồng hồ cửa hàng chạy nhanh — dung sai 1 ngày (lệch hơn thế là tự nó đã là sự cố, và audit
     L4 sẽ thấy đơn thiếu). Mỗi file bronze nằm ở phân vùng `_dt` = ngày ĐẦU cửa sổ của nó, nên
     dòng có `recorded_at ≥ T` chỉ nằm trong file có mép cuối cửa sổ > T — tra `bronze_load_log`
     ra `_dt` nhỏ nhất của các file đó, và ClickHouse bỏ qua mọi phân vùng tháng cũ hơn. Không
     giả định gì về độ dài cửa sổ (giờ ở production, tháng ở `simulator bulk`).

  Mọi hằng số (tập tháng, mép `_dt`) tính MỘT lần lúc dbt dựng model (`run_query`) và nhúng vào
  SQL, cùng lý do với `pipeline_cutoff()`: hai lần tính trong một lượt có thể ra hai mép khác nhau.

  ## Dựng lại một khoảng (backfill)

  `dbt run --select fact_sale_line --vars '{rebuild_months: [202501, 202502]}'` thay trọn các tháng
  đó bằng dữ liệu đang có ở bronze. Lần dựng đầu (bảng chưa có) ở quy mô lớn cũng nên đi từng
  tháng như vậy thay vì một câu cho 24 tháng.
-#}

{%- macro _scalar(sql) -%}
{%- if execute -%}
    {%- set rows = run_query(sql).rows -%}
    {{ return(rows[0][0] if rows else none) }}
{%- else -%}
    {{ return(none) }}
{%- endif -%}
{%- endmacro %}

{#- `_dt` nhỏ nhất của file bảng `table` có thể chứa dòng với mốc ghi nhận ≥ `ts_sql`. -#}
{%- macro min_dt_since(table, ts_sql) -%}
{%- set value = _scalar(
    "SELECT toString(ifNull(toDate(min(window_start)), toDate('2100-01-01')))"
    ~ " FROM " ~ source('bronze', 'bronze_load_log')
    ~ " WHERE table_name = '" ~ table ~ "' AND window_end > " ~ ts_sql
) -%}
toDate('{{ value or "1970-01-01" }}')
{%- endmacro %}

{#-
  Tập tháng (`YYYYMM` của `occurred_at`) cần dựng lại cho `this`: tháng của mọi dòng bronze
  MỚI (mốc ghi nhận lớn hơn mốc lớn nhất đã có trong fact, dưới `cutoff`) — bẫy 5 (docs/17 §4).
  Đọc thẳng bronze, không qua staging: có một dòng mới là tháng đó bị ảnh hưởng, không cần biết
  nó có phải phiên bản mới nhất không. Trả `none` = dựng toàn bộ (bảng chưa tồn tại).
-#}
{%- macro affected_month_list(table, version_col, cutoff) -%}
{%- if var('rebuild_months', none) is not none -%}
    {{ return(var('rebuild_months') | list) }}
{%- endif -%}
{%- if not is_incremental() -%}
    {{ return(none) }}
{%- endif -%}
{%- set built = "(SELECT max(_recorded_at) FROM " ~ this ~ ")" -%}
{%- set sql -%}
    SELECT DISTINCT toYYYYMM(occurred_at) FROM {{ source('bronze', 'bronze_' ~ table) }}
    WHERE {{ version_col }} < {{ cutoff }} AND {{ version_col }} > {{ built }}
      AND _dt >= {{ min_dt_since(table, built) }}
    ORDER BY 1
{%- endset -%}
{%- if execute -%}
    {#- `.rows`, không `.columns[0]`: kết quả RỖNG của adapter ClickHouse không có cả định nghĩa
        cột — lượt "không có gì mới" (thường gặp nhất) chết ở đây ở bản đầu. -#}
    {%- set months = [] -%}
    {%- for row in run_query(sql).rows -%}
        {%- do months.append(row[0] | int) -%}
    {%- endfor -%}
    {{ return(months) }}
{%- else -%}
    {{ return([]) }}
{%- endif -%}
{%- endmacro %}

{#- Mốc ghi nhận sớm nhất mà một dòng có `occurred_at` thuộc các tháng `months` có thể mang. -#}
{%- macro _earliest_recorded(months) -%}
{%- set first = months | min | string -%}
toDateTime64('{{ first[:4] }}-{{ first[4:] }}-01 00:00:00', 6, 'UTC') - INTERVAL 1 DAY
{%- endmacro %}

{#- `AND` lọc tháng + cắt phân vùng `_dt` cho bảng bronze `table`; rỗng khi dựng toàn bộ. -#}
{%- macro month_filter(table, months) -%}
{%- if months is not none %}
  AND toYYYYMM(occurred_at) IN ({{ (months | join(', ')) or '0' }})
  {%- if months %}
  AND _dt >= {{ min_dt_since(table, _earliest_recorded(months)) }}
  {%- endif %}
{%- endif %}
{%- endmacro %}

{#- Cắt phân vùng `_dt` cho BẢNG CON (dòng hàng, thanh toán): chúng đi cùng cửa sổ với đơn cha. -#}
{%- macro child_dt_filter(table, months) -%}
{%- if months %}
  AND _dt >= {{ min_dt_since(table, _earliest_recorded(months)) }}
{%- endif %}
{%- endmacro %}

{#- Phiên bản MỚI NHẤT của mỗi đơn trong các tháng `months`, dưới `cutoff` — thay `stg_sale`. -#}
{%- macro latest_sales(cutoff, months) -%}
SELECT * EXCEPT (_dt, _source_file)
FROM {{ source('bronze', 'bronze_sale') }}
WHERE recorded_at < {{ cutoff }}{{ month_filter('sale', months) }}
ORDER BY sale_id, recorded_at DESC
LIMIT 1 BY sale_id
{%- endmacro %}
