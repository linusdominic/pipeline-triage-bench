# Freshness alerts

A freshness monitor fires when the newest partition of a table is older than the monitor
expects. The monitor's `calendar` setting decides what "expected" means: `weekdays`
knows weekends only; `business_calendar` also knows bank holidays.

1. Which partition is missing? Compare the alert's expected partition with the business
   calendar and the source contract's delivery schedule and holiday rule.
2. Was a delivery expected for that partition? If not, the alert is expected: close it.
3. Is the file in the landing zone? Check its arrival time against the DAG run time
   (06:00) in the task runs and logs.
   - It arrived after the run: backfill the partition.
   - It is not there and it is overdue: escalate to the source owner.
4. Did the ingest task fail for another reason? See task_failure.md.
