-- More principal paid than lent means payments were loaded wrongly.
select ds, loan_id, outstanding_balance
from {{ ref('fct_loan_balance_daily') }}
where outstanding_balance < -0.01
