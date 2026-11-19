select
    p.ds,
    p.payment_id,
    p.loan_id,
    l.borrower_id,
    l.branch_code,
    b.region,
    l.product_code,
    p.payment_date,
    p.amount,
    p.principal_amount,
    p.interest_amount
from {{ ref('stg_payments') }} as p
left join {{ ref('stg_loans') }} as l
    on l.loan_id = p.loan_id
left join {{ ref('dim_branch') }} as b
    on b.branch_code = l.branch_code
where {{ partition_predicate('p.ds') }}
