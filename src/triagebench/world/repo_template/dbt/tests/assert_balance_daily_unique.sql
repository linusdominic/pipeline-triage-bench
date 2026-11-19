-- One row per loan per day.
select ds, loan_id, count(*) as n
from {{ ref('fct_loan_balance_daily') }}
group by ds, loan_id
having count(*) > 1
