-- Daily delinquency by region and product, published to finance each morning.
select
    f.ds,
    coalesce(b.region, 'UNMAPPED') as region,
    f.product_code,
    count(*) as active_loans,
    cast(sum(f.outstanding_balance) as decimal(18, 2)) as outstanding_balance,
    count(*) filter (where f.days_since_payment > 35) as delinquent_loans,
    cast(coalesce(sum(f.outstanding_balance) filter (where f.days_since_payment > 35), 0)
        as decimal(18, 2)) as delinquent_balance
from {{ ref('fct_loan_balance_daily') }} as f
left join {{ ref('dim_branch') }} as b
    on b.branch_code = f.branch_code
where {{ partition_predicate('f.ds') }}
  and f.outstanding_balance > 0
group by f.ds, coalesce(b.region, 'UNMAPPED'), f.product_code
