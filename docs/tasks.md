# Task families

Every task starts from the base world rewound to the business day before its incident window.
The scheduler then runs each day of the window with the task's fault or benign condition
applied, the monitors run at 07:30 after each run, and the episode starts at 08:30 on the
calendar day after the incident partition. Definitions are in
[src/triagebench/tasks/catalog.py](../src/triagebench/tasks/catalog.py).

Agents never see this file or anything else in this repository: an episode is a copy of a task
world in a temporary directory, and the tools only reach that copy. The leakage check in
`make validate` confirms that no task id, family id, task summary or decision identifier is
reachable through the tools.

"Noise" marks a variant with an unrelated benign alert open as well: some payment CDC events
were re-sent and the payments volume monitor fires, while staging removes the duplicates.

## Late vendor file (`late_file`)

A vendor file arrived after the 06:00 run. The ingest task failed on all three attempts with
`MissingDeliveryError`, `dbt_build` and `publish` did not run, and the file is now in landing.

- Decision: `backfill`. Root cause: the source.
- Expected end state: the partition as if the file had arrived on time.
- Acceptable: backfill of that partition (whole DAG or the ingest task), re-running the
  ingest task or its downstream tasks for that partition.

| Task | Source | Incident partition | Noise |
| --- | --- | --- | --- |
| late_file_a | payments | 2026-11-05 | |
| late_file_b | bureau (weekly) | 2026-11-09 | |
| late_file_c | payments | 2026-10-21 | yes |

## Freshness alert on a bank holiday (`holiday`)

No run happens for a bank holiday and no vendor file is due, as the contracts say. The
freshness monitors use a weekday calendar, so they expect a partition anyway.

- Decision: `no_action`. Root cause: `source:calendar`; `source:payments` and the alerting
  freshness monitors are accepted.
- Expected end state: unchanged.
- Acceptable: switching an alerting freshness monitor to `calendar: business_calendar`.

| Task | Holiday | Alerts |
| --- | --- | --- |
| holiday_a | Veterans Day, Wednesday 2026-11-11 | payments and mart freshness |
| holiday_b | Columbus Day, Monday 2026-10-12 | payments, weekly bureau and mart freshness |
| holiday_c | Labor Day, Monday 2026-09-07 | payments, weekly bureau and mart freshness |

## Transient infrastructure or worker failure (`transient`)

A task failed on every attempt for a reason unrelated to its input; the input is fine.

- Decision: `rerun`. Root cause: `infrastructure` or `orchestration`.
- Expected end state: the partition as if the run had succeeded.

| Task | Failed task | Error in the log | Root cause | Noise |
| --- | --- | --- | --- | --- |
| transient_a | dbt_build | DuckDB lock held by another process | infrastructure | |
| transient_b | ingest_payments | read timeout on the landing share | infrastructure | |
| transient_c | dbt_build | worker evicted, SIGKILL | orchestration | yes |

## Join fan-out from a recent commit (`fanout`)

An engineer's commit the afternoon before changed a model so that it produces duplicate rows.
A uniqueness test fails, `dbt build` skips the models downstream of the test, and the build
fails on both attempts.

- Decision: `fix_code`. Root cause: the changed model.
- Expected end state: every affected partition rebuilt with the original logic.
- Acceptable: reverting the commit, or a patch to the same file; rebuilding the affected
  partitions.
- dbt tests check whole tables. When two partitions are affected, the first rebuild still sees
  the other partition's duplicates and skips the mart; it has to be rebuilt once more. The
  dbt test alert stays open until it is, so an agent that checks its work sees it.

| Task | Model | Change | Affected partitions | Noise |
| --- | --- | --- | --- | --- |
| fanout_a | fct_payments | region read from the raw branch feed (one row per weekly snapshot) | 1 | |
| fanout_b | fct_loan_balance_daily | score band joined from every bureau refresh | 2 | yes |
| fanout_c | stg_payments | CDC deduplication keyed on event, not payment | 1 | |

## Amounts in cents instead of dollars (`cents`)

The servicer's file has amounts in cents for some or all rows; the contract says dollars and
the trailer's control total is in dollars. The data is loaded; the reconciliation monitor
fires and the negative-balance test fails.

- Decision: `quarantine_and_escalate`. Root cause: `source:payments`.
- Expected end state: the raw partition kept as delivered and quarantined, the models for that
  partition rebuilt without it, an escalation to the servicer.
- Forbidden: correcting the data (write SQL, editing the file, or changing the pipeline to
  divide by 100).

| Task | Partition | Rows in cents |
| --- | --- | --- |
| cents_a | 2026-11-02 | all |
| cents_b | 2026-10-15 | about 40% |
| cents_c | 2026-09-23 | all |

## Column renamed without notice (`rename`)

A vendor renamed a column without the 30 days' notice the contract requires. Ingestion fails
with a schema mismatch; the schema monitor fires.

- Decision: `escalate`. Root cause: the source.
- Expected end state: unchanged, and an escalation to the vendor.
- Patching the mapping is scored as unnecessary and, because it loads the file, breaks the
  end state.

| Task | Source | Rename | Notice | Noise |
| --- | --- | --- | --- | --- |
| rename_a | payments | amount to payment_amount | none | |
| rename_b | bureau | score to fico_score | 5 days, in announcements | |
| rename_c | payments | loan_id to loan_number | none | yes |

## Volume drop from an announced discontinuation (`discontinuation`)

Products stopped being originated on a date announced weeks earlier. The originations volume
monitor fires.

- Decision: `no_action`. Root cause: `source:calendar` (the announcements); `source:loans` is
  accepted.
- Acceptable: setting the volume monitor's `baseline_start` to the change date.

| Task | Products | Effective | Announced |
| --- | --- | --- | --- |
| discontinuation_a | HELOC | 2026-11-09 | 2026-10-14 |
| discontinuation_b | AUTO60, PERS36 | 2026-10-20 | 2026-09-30 |
| discontinuation_c | HELOC, PERS36 | 2026-09-21 | 2026-09-02 |

## Monitor threshold made too tight (`tight_config`)

A commit to `monitoring/monitors.yml` the day before made a monitor fire on normal data.

- Decision: `fix_config`. Root cause: the monitor.
- Expected end state: warehouse unchanged; the parameter back in a sensible range.
- Acceptable: `update_monitor` with a value in range, or reverting the config commit.

| Task | Monitor | Change |
| --- | --- | --- |
| tight_config_a | volume_raw_payments | threshold 0.35 to 0.05 |
| tight_config_b | freshness_raw_payments | max_partition_lag_days 1 to 0 |
| tight_config_c | volume_raw_loans | threshold 0.30 to 0.06 |

## CDC replay duplicates (`cdc_replay`)

The servicer's connector re-sent events with their original sequence numbers. The raw table
grows; staging keeps the latest event per payment, so the marts are unaffected.

- Decision: `no_action`. Root cause: `source:payments`.

| Task | Partition | Events re-sent |
| --- | --- | --- |
| cdc_replay_a | 2026-11-03 | all |
| cdc_replay_b | 2026-10-29 | 45% |
| cdc_replay_c | 2026-09-30 | 80% |

## Stale branch reference data (`stale_reference`)

The weekly branch snapshot, which includes a branch opening that week, arrived after the
weekly `ingest_branches` run. That run loaded nothing new and succeeded. Loans at the new
branch then fail the relationship test (a warning) and appear under `UNMAPPED` in the
delinquency mart.

- Decision: `backfill`. Root cause: `source:branches`.
- Expected end state: `dim_branch` with the new branch and the affected partitions rebuilt.
- Acceptable: re-running `ingest_branches` for the week's first business day and rebuilding
  the partitions of that week up to the incident.

| Task | Snapshot week | New branches | Noise |
| --- | --- | --- | --- |
| stale_reference_a | 2026-11-02 | one | |
| stale_reference_b | 2026-10-19 | one | |
| stale_reference_c | 2026-10-05 | two | yes |

## Truncated vendor file (`truncated`)

The file holds fewer records than its trailer says, so ingestion fails. In two variants the
sender has already put a complete re-delivery (`_r1`) in landing; in one it has not.

- With a re-delivery: `rerun` the ingest; root cause the source; end state as if the complete
  file had arrived on time.
- Without: `escalate` to the owner; end state unchanged.

| Task | Source | Records kept | Re-delivery | Noise |
| --- | --- | --- | --- | --- |
| truncated_a | payments | 60% | yes | |
| truncated_b | payments | 55% | no | |
| truncated_c | loans | 50% | yes | yes |
