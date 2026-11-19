-- Warehouse schemas outside dbt. Applied once when the warehouse is created.
create schema if not exists raw;
create schema if not exists ops;

create table if not exists raw.loans (
    loan_id varchar, borrower_id varchar, branch_code varchar, product_code varchar,
    origination_date date, principal decimal(18, 2), annual_rate double, term_months integer,
    _ds date, _file varchar, _row integer, _loaded_at timestamp
);
create table if not exists raw.payments (
    payment_id varchar, loan_id varchar, payment_date date, amount decimal(18, 2),
    principal_amount decimal(18, 2), interest_amount decimal(18, 2), cdc_op varchar,
    cdc_seq integer, _ds date, _file varchar, _row integer, _loaded_at timestamp
);
create table if not exists raw.bureau (
    borrower_id varchar, score integer, score_date date,
    _ds date, _file varchar, _row integer, _loaded_at timestamp
);
create table if not exists raw.branches (
    branch_code varchar, branch_name varchar, region varchar, opened_date date,
    _snapshot_date date, _file varchar, _row integer, _loaded_at timestamp
);
create table if not exists ops.quarantine (
    source_table varchar, ds date, reason varchar, quarantined_at timestamp
);
create table if not exists ops.dbt_test_results (
    run_id varchar, ds date, test_name varchar, model varchar, status varchar,
    failures integer, message varchar
);
create table if not exists ops.published_partitions (
    ds date, run_id varchar, published_at timestamp
);
