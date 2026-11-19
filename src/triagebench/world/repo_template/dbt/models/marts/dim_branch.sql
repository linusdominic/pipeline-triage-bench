{{ config(materialized='table', pre_hook=[]) }}

-- Branch attributes never change between snapshots; new branches are added.
select
    branch_code,
    branch_name,
    region,
    opened_date,
    min(_snapshot_date) as first_snapshot_date
from {{ source('raw', 'branches') }}
group by branch_code, branch_name, region, opened_date
