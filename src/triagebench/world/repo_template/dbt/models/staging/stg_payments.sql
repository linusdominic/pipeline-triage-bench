-- Keel sends one record per CDC event. The event with the highest cdc_seq is the
-- current state of a payment; replayed events repeat an existing cdc_seq.
with source as (
    select *
    from {{ source('raw', 'payments') }}
    where {{ partition_predicate('_ds') }}
      and {{ not_quarantined('_ds', 'raw.payments') }}
),

ranked as (
    select
        *,
        row_number() over (
            partition by payment_id
            order by cdc_seq desc, _row desc
        ) as rn
    from source
)

select
    _ds as ds,
    payment_id,
    loan_id,
    payment_date,
    amount,
    principal_amount,
    interest_amount
from ranked
where rn = 1
