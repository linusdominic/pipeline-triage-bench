with source as (
    select *
    from {{ source('raw', 'loans') }}
    where {{ partition_predicate('_ds') }}
      and {{ not_quarantined('_ds', 'raw.loans') }}
),

ranked as (
    select
        *,
        row_number() over (partition by loan_id order by _row desc) as rn
    from source
)

select
    _ds as ds,
    loan_id,
    borrower_id,
    upper(trim(branch_code)) as branch_code,
    product_code,
    origination_date,
    principal,
    annual_rate,
    term_months
from ranked
where rn = 1
