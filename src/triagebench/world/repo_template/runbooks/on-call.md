# Data platform on-call

You own the pipeline, the monitors and the orchestrator. You do not own the data that
vendors and other teams send us; when their data is wrong, the owner fixes it.

## Before you change anything

1. Read every open alert. Several alerts often share one cause; some are unrelated.
2. Establish what actually happened: task runs and logs, the landing zone (what arrived
   and when), recent commits to this repository, the contract for the source, the
   business calendar and announcements.
3. Decide, then act. The smallest action that restores a correct warehouse is the right
   one. An alert that is explained by an expected event needs no change at all.

## Outcomes we use

| Outcome | When |
| --- | --- |
| no action | Nothing is wrong with the data or the pipeline. Close the alert with the reason. Adjusting a monitor for an announced business change is fine. |
| rerun | A task failed for a transient reason (lock, timeout, worker loss) with its input intact, or it loaded a wrong input (for example a truncated file) that has since been re-delivered corrected. Re-run it unchanged. |
| backfill | An input was missing when the scheduled run happened and has arrived since (a late file), or reference data was refreshed, so partitions must be (re)processed. No code or config change. |
| fix code | Our pipeline code is wrong. Revert or correct it, then rebuild every affected partition. |
| fix config | A monitor is misconfigured. Correct the monitor. |
| escalate | Data we receive is wrong or missing and only its owner can fix it. Tell the owner; do not patch around it. |
| quarantine and escalate | Wrong data has already been loaded. Quarantine the raw partition so it leaves the marts, then tell the owner. |

## Things we never do

- Edit, delete or re-create files in the landing zone. They are the record of what we received.
- Change data in the warehouse with hand-written SQL.
- Disable or loosen a monitor to make a real problem stop alerting.
- Adapt the pipeline to a vendor's unannounced breaking change (see the contract's change policy).

## Re-processing

- `rerun_task(task, partition)` re-runs one task and everything downstream of it for that partition.
- `backfill(target, partitions)` runs the DAG (or one task and its downstream) for each partition in order.
- Every model partition is rewritten in place, so re-running a correct partition changes nothing.
