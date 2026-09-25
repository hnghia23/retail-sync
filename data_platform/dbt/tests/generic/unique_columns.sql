{#- Tổ hợp cột là duy nhất — thay dbt_utils.unique_combination_of_columns (không kéo package). -#}
{% test unique_columns(model, columns) %}
SELECT {{ columns | join(', ') }}, count() AS n
FROM {{ model }}
GROUP BY {{ columns | join(', ') }}
HAVING n > 1
{% endtest %}
