-- One row per loan per business-day partition: outstanding principal and days since
-- the last payment, as of the end of that day.
with days as (
    {% if var('ds', none) is not none %}
    select cast('{{ var("ds") }}' as date) as ds
    {% else %}
    select distinct ds from {{ ref('stg_loans') }}
    {% endif %}
),

paid as (
    select
        d.ds,
        p.loan_id,
        sum(p.principal_amount) as principal_paid,
        max(p.payment_date) as last_payment_date
    from days as d
    inner join {{ ref('fct_payments') }} as p
        on p.ds <= d.ds
    group by d.ds, p.loan_id
)

select
    d.ds,
    l.loan_id,
    l.borrower_id,
    l.branch_code,
    l.product_code,
    l.principal,
    cast(l.principal - coalesce(pd.principal_paid, 0) as decimal(18, 2)) as outstanding_balance,
    coalesce(pd.last_payment_date, l.origination_date) as last_payment_date,
    date_diff('day', coalesce(pd.last_payment_date, l.origination_date), d.ds) as days_since_payment
from days as d
inner join {{ ref('stg_loans') }} as l
    on l.ds <= d.ds
left join paid as pd
    on pd.ds = d.ds and pd.loan_id = l.loan_id
