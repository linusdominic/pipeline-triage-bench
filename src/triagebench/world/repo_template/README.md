# harbor-data-pipeline

Ingestion, dbt models and monitoring for the Harbor Lending warehouse (DuckDB).

| Path | What it is |
| --- | --- |
| `ingestion/harbor_ingest.py` | Loads landing-zone files into `raw.*`, one business-day partition per run |
| `contracts/` | Data contracts for each source: columns, units, delivery schedule, change policy |
| `dbt/` | dbt project: `staging` (deduplicated sources) and `marts` (facts, dimension, delinquency mart) |
| `monitoring/monitors.yml` | Freshness, volume, schema, reconciliation, dbt-test and task-failure monitors |
| `runbooks/` | What to do when a monitor alerts |
| `owners.yaml` | Who owns each source and component |
| `warehouse/ddl.sql` | Raw and ops tables that live outside dbt |

The daily DAG `harbor_daily` runs at 06:00 for the previous business day `ds`:
`ingest_loans`, `ingest_payments`, `ingest_bureau` (weekly), `ingest_branches` (weekly)
then `dbt_build` (`dbt build --vars '{ds: <ds>}'`) and `publish`. It does not run for
weekends or bank holidays.

Every incremental model is partitioned by `ds`; a build for one `ds` deletes and rewrites
that partition only, so re-running a partition is safe.
