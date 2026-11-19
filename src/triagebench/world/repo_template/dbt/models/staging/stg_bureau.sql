with source as (
    select *
    from {{ source('raw', 'bureau') }}
    where {{ partition_predicate('_ds') }}
      and {{ not_quarantined('_ds', 'raw.bureau') }}
),

ranked as (
    select
        *,
        row_number() over (partition by _ds, borrower_id order by _row desc) as rn
    from source
)

select
    _ds as ds,
    borrower_id,
    score,
    score_date
from ranked
where rn = 1
