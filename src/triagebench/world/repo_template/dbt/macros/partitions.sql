{#- Partition helpers. Every incremental model is partitioned by `ds` (a business day). -#}

{% macro partition_predicate(column) -%}
  {%- if var('ds', none) is not none -%}
    {{ column }} = cast('{{ var("ds") }}' as date)
  {%- else -%}
    true
  {%- endif -%}
{%- endmacro %}

{% macro delete_partition() -%}
  {%- if is_incremental() and var('ds', none) is not none -%}
    delete from {{ this }} where ds = cast('{{ var("ds") }}' as date)
  {%- else -%}
    select 1
  {%- endif -%}
{%- endmacro %}

{% macro not_quarantined(column, source_table) -%}
  {{ column }} not in (
    select ds from {{ source('ops', 'quarantine') }} where source_table = '{{ source_table }}'
  )
{%- endmacro %}
