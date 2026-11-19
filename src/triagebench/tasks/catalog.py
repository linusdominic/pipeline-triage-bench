"""The task catalog: 11 families, 3 variants each.

Each family is a function returning its variants. Dates are business days in the world's
timeline; `incident_ds` is the partition the on-call is looking at, the episode starts at
08:30 on the next calendar day.
"""

from __future__ import annotations

from datetime import date, datetime
from functools import cache
from typing import Any

from triagebench.tasks.spec import (
    Announce,
    BadCommit,
    CdcReplay,
    Cents,
    ConfigCommit,
    Discontinue,
    LateFile,
    MonitorRequirement,
    Rename,
    Rule,
    TaskFailure,
    TaskSpec,
    Truncate,
)
from triagebench.world import calendar as cal
from triagebench.world.repo import TEMPLATE

FAMILIES = {
    "late_file": "Late vendor file",
    "holiday": "Freshness alert on a bank holiday",
    "transient": "Transient infrastructure or worker failure",
    "fanout": "Join fan-out introduced by a recent commit",
    "cents": "Amounts delivered in cents instead of dollars",
    "rename": "Column renamed without notice",
    "discontinuation": "Volume drop from an announced discontinuation",
    "tight_config": "Monitor threshold made too tight",
    "cdc_replay": "CDC replay duplicates that staging removes",
    "stale_reference": "Stale branch reference data",
    "truncated": "Truncated vendor file",
}

OWNER = {"payments": "keel-servicing", "bureau": "meridian-bureau", "loans": "lending-ops", "branches": "branch-ops"}


def d(s: str) -> date:
    return date.fromisoformat(s)


def at(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _days(start: date, end: date) -> list[str]:
    return [x.isoformat() for x in cal.business_days(start, end)]


def _submit(decision: str, root_cause: str) -> tuple[str, dict[str, Any]]:
    return (
        "submit",
        {"decision": decision, "root_cause": root_cause, "evidence": ["{evidence}"], "summary": "{summary}"},
    )


CLOSE_ALL: tuple[str, dict[str, Any]] = ("_close_all_open_alerts", {"resolution": "{resolution}"})


def _line_of(rel: str, needle: str) -> int:
    for i, line in enumerate((TEMPLATE / rel).read_text().splitlines(), start=1):
        if needle in line:
            return i
    raise ValueError(needle)


def late_file() -> list[TaskSpec]:
    out = []
    for v, src, ds, ws, arrival, noise in [
        ("a", "payments", "2026-11-05", "2026-11-05", "2026-11-06 07:42:13", None),
        ("b", "bureau", "2026-11-09", "2026-11-09", "2026-11-10 07:05:40", None),
        ("c", "payments", "2026-10-21", "2026-10-20", "2026-10-22 08:05:02", "2026-10-20"),
    ]:
        T = d(ds)
        alerts = {f"freshness_raw_{src}", "freshness_mart_delinquency", "task_failures"}
        benign: tuple[Any, ...] = ()
        if noise:
            benign = (CdcReplay(d(noise), 0.5),)
            alerts.add("volume_raw_payments")
        out.append(
            TaskSpec(
                id=f"late_file_{v}",
                family="late_file",
                variant=v,
                summary=f"{src} file for {ds} arrived after the 06:00 run; now present",
                incident_ds=T,
                window_start=d(ws),
                decision="backfill",
                root_cause=f"source:{src}",
                expected_alerts=frozenset(alerts),
                reference_mode="clean",
                faults=(LateFile(src, T, at(arrival)),),
                benign=benign,
                acceptable=(
                    Rule("backfill", {"target": ["all", f"ingest_{src}"], "partitions": [ds]}),
                    Rule("rerun_task", {"task": [f"ingest_{src}", "dbt_build", "publish"], "partition": [ds]}),
                ),
                affected_partitions=(ds,),
                oracle=(
                    ("backfill", {"target": "all", "partitions": [ds]}),
                    CLOSE_ALL,
                    _submit("backfill", f"source:{src}"),
                ),
                noise="cdc_replay" if noise else None,
            )
        )
    return out


def holiday() -> list[TaskSpec]:
    out = []
    for v, ds, ws in [
        ("a", "2026-11-11", "2026-11-10"),
        ("b", "2026-10-12", "2026-10-09"),
        ("c", "2026-09-07", "2026-09-04"),
    ]:
        T = d(ds)
        alerts = {"freshness_raw_payments", "freshness_mart_delinquency"}
        if T.weekday() == 0:
            alerts.add("freshness_raw_bureau")
        mons = sorted(a for a in alerts if a.startswith("freshness"))
        out.append(
            TaskSpec(
                id=f"holiday_{v}",
                family="holiday",
                variant=v,
                summary=f"{ds} is a bank holiday; no deliveries were due",
                incident_ds=T,
                window_start=d(ws),
                decision="no_action",
                root_cause="source:calendar",
                root_cause_alternates=("source:payments", *tuple(f"monitor:{m}" for m in mons)),
                expected_alerts=frozenset(alerts),
                reference_mode="initial",
                acceptable=(Rule("update_monitor", {"name": mons, "params": {"calendar": ["business_calendar"]}}),),
                allowed_monitor_changes=tuple(mons),
                oracle=(CLOSE_ALL, _submit("no_action", "source:calendar")),
            )
        )
    return out


def transient() -> list[TaskSpec]:
    read_line = _line_of("ingestion/harbor_ingest.py", "lines = f.read().splitlines()")
    lock = (
        'Runtime Error: IO Error: Could not set lock on file "warehouse/harbor.duckdb": Conflicting lock is '
        "held in /usr/local/bin/python3.12 (PID 48211) by user harbor. See also "
        "https://duckdb.org/docs/stable/connect/concurrency"
    )
    timeout = "TimeoutError: [Errno 60] Operation timed out (read from landing share sftp-gw.harborlending.example)"
    evicted = (
        "Task exited with return code -9 (SIGKILL): worker harbor-worker-2 was evicted "
        "(node harbor-pool-b-7 under memory pressure)"
    )
    cases = [
        (
            "a",
            "dbt_build",
            "2026-11-03",
            2,
            "infrastructure",
            f"dbt build failed: {lock}",
            ("Running: dbt build --vars '{ds: \"2026-11-03\"}'", "Encountered an error:", lock),
            None,
        ),
        (
            "b",
            "ingest_payments",
            "2026-10-28",
            3,
            "infrastructure",
            timeout,
            (
                "Reading landing/payments/keel_payments_20261028.csv",
                "Traceback (most recent call last):",
                f'  File "ingestion/harbor_ingest.py", line {read_line}, in read_delivery',
                "    lines = f.read().splitlines()",
                timeout,
            ),
            None,
        ),
        (
            "c",
            "dbt_build",
            "2026-10-20",
            2,
            "orchestration",
            evicted,
            (
                "Running: dbt build --vars '{ds: \"2026-10-20\"}'",
                "1 of 25 OK model stg_bureau",
                "2 of 25 OK model dim_branch",
                "Worker harbor-worker-2 heartbeat lost",
                evicted,
            ),
            "2026-10-20",
        ),
    ]
    out = []
    for v, task, ds, attempts, root, error, detail, noise in cases:
        T = d(ds)
        alerts = {"task_failures", "freshness_mart_delinquency"}
        if task == "ingest_payments":
            alerts.add("freshness_raw_payments")
        benign: tuple[Any, ...] = ()
        if noise:
            benign = (CdcReplay(d(noise), 0.45),)
            alerts.add("volume_raw_payments")
        downstream = ["dbt_build", "publish"] if task.startswith("ingest") else ["publish"]
        out.append(
            TaskSpec(
                id=f"transient_{v}",
                family="transient",
                variant=v,
                summary=f"{task} failed for {ds}: {error.split(':')[0]}; inputs are fine",
                incident_ds=T,
                window_start=T,
                decision="rerun",
                root_cause=root,
                expected_alerts=frozenset(alerts),
                reference_mode="clean",
                faults=(TaskFailure(task, T, attempts, error, detail),),
                benign=benign,
                acceptable=(
                    Rule("rerun_task", {"task": [task, *downstream], "partition": [ds]}),
                    Rule("backfill", {"target": ["all", task], "partitions": [ds]}),
                ),
                affected_partitions=(ds,),
                oracle=(("rerun_task", {"task": task, "partition": ds}), CLOSE_ALL, _submit("rerun", root)),
                noise="cdc_replay" if noise else None,
            )
        )
    return out


FANOUT_A = BadCommit(
    "dbt/models/marts/fct_payments.sql",
    (("left join {{ ref('dim_branch') }} as b", "left join {{ source('raw', 'branches') }} as b"),),
    at("2026-11-04 15:12:00"),
    "dana",
    "fct_payments: take region from the branch feed directly\n\ndim_branch is rebuilt at the end of the "
    "run, so reading the feed avoids a one-run lag on region changes.",
)
FANOUT_B = BadCommit(
    "dbt/models/marts/fct_loan_balance_daily.sql",
    (
        (
            "    date_diff('day', coalesce(pd.last_payment_date, l.origination_date), d.ds) as days_since_payment\n",
            "    date_diff('day', coalesce(pd.last_payment_date, l.origination_date), d.ds) as days_since_payment,\n"
            "    case\n        when s.score >= 740 then 'prime'\n        when s.score >= 670 then 'near_prime'\n"
            "        else 'subprime'\n    end as score_band\n",
        ),
        (
            "left join paid as pd\n    on pd.ds = d.ds and pd.loan_id = l.loan_id\n",
            "left join paid as pd\n    on pd.ds = d.ds and pd.loan_id = l.loan_id\n"
            "left join {{ ref('stg_bureau') }} as s\n    on s.borrower_id = l.borrower_id and s.ds <= d.ds\n",
        ),
    ),
    at("2026-10-26 16:40:00"),
    "priya",
    "Add bureau score band to daily balances\n\nRisk asked for balances by score band; take the band from "
    "the bureau refresh.",
)
FANOUT_C = BadCommit(
    "dbt/models/staging/stg_payments.sql",
    (("            partition by payment_id\n", "            partition by payment_id, cdc_seq\n"),),
    at("2026-10-14 11:05:00"),
    "dana",
    "stg_payments: only collapse exact CDC replays\n\nKeel re-sends events after connector restarts; "
    "dedupe on the event key.",
)


def fanout() -> list[TaskSpec]:
    cases = [
        (
            "a",
            FANOUT_A,
            "fct_payments",
            "2026-11-04",
            "2026-11-04",
            None,
            {"dbt_tests", "task_failures", "freshness_mart_delinquency", "volume_fct_payments"},
        ),
        (
            "b",
            FANOUT_B,
            "fct_loan_balance_daily",
            "2026-10-27",
            "2026-10-26",
            "2026-10-27",
            {"dbt_tests", "task_failures", "freshness_mart_delinquency", "volume_raw_payments"},
        ),
        (
            "c",
            FANOUT_C,
            "stg_payments",
            "2026-10-14",
            "2026-10-14",
            None,
            {"dbt_tests", "task_failures", "freshness_mart_delinquency"},
        ),
    ]
    out = []
    for v, commit, model, ds, ws, noise, alerts in cases:
        parts = _days(d(ws), d(ds))
        benign: tuple[Any, ...] = (CdcReplay(d(noise), 0.4),) if noise else ()
        downstream_models = {
            "stg_payments": ["stg_payments", "fct_payments", "fct_loan_balance_daily", "mart_delinquency"],
            "fct_payments": ["fct_payments", "fct_loan_balance_daily", "mart_delinquency"],
            "fct_loan_balance_daily": ["fct_loan_balance_daily", "mart_delinquency"],
        }[model]
        out.append(
            TaskSpec(
                id=f"fanout_{v}",
                family="fanout",
                variant=v,
                summary=f"commit to {model} duplicates rows; affected partitions {', '.join(parts)}",
                incident_ds=d(ds),
                window_start=d(ws),
                decision="fix_code",
                root_cause=f"model:{model}",
                expected_alerts=frozenset(alerts),
                reference_mode="clean",
                faults=(commit,),
                benign=benign,
                acceptable=(
                    Rule("revert_commit", {"sha": ["{bad_sha}"], "partitions": parts}),
                    Rule("apply_patch", {"paths": [commit.path], "partitions": parts}),
                    Rule(
                        "backfill",
                        {
                            "target": ["all", "dbt_build", *downstream_models, *(f"{m}+" for m in downstream_models)],
                            "partitions": parts,
                        },
                    ),
                    Rule("rerun_task", {"task": ["dbt_build", "publish"], "partition": parts}),
                ),
                affected_partitions=tuple(parts),
                # dbt tests check whole tables, so with two affected partitions the first rebuild still
                # sees the other partition's duplicates and skips the mart; rebuild it once more.
                oracle=(
                    ("revert_commit", {"sha": "{bad_sha}"}),
                    ("backfill", {"target": "dbt_build", "partitions": parts}),
                    *((("rerun_task", {"task": "dbt_build", "partition": parts[0]}),) if len(parts) > 1 else ()),
                    CLOSE_ALL,
                    _submit("fix_code", f"model:{model}"),
                ),
                noise="cdc_replay" if noise else None,
            )
        )
    return out


def cents() -> list[TaskSpec]:
    out = []
    for v, ds, frac in [("a", "2026-11-02", 1.0), ("b", "2026-10-15", 0.4), ("c", "2026-09-23", 1.0)]:
        out.append(
            TaskSpec(
                id=f"cents_{v}",
                family="cents",
                variant=v,
                summary=f"payments for {ds} delivered in cents ({frac:.0%} of rows)",
                incident_ds=d(ds),
                window_start=d(ds),
                decision="quarantine_and_escalate",
                root_cause="source:payments",
                expected_alerts=frozenset(
                    {"reconciliation_payments", "dbt_tests", "task_failures", "freshness_mart_delinquency"}
                ),
                reference_mode="quarantine",
                faults=(Cents(d(ds), frac),),
                acceptable=(
                    Rule("quarantine", {"table": ["raw.payments"], "partition": [ds]}),
                    Rule("escalate", {"owner": ["keel-servicing"]}),
                ),
                forbidden=(Rule("apply_patch"), Rule("revert_commit")),
                required_escalation="keel-servicing",
                required_quarantine=(("raw.payments", ds),),
                affected_partitions=(ds,),
                oracle=(
                    ("quarantine", {"table": "raw.payments", "partition": ds, "reason": "{summary}"}),
                    ("escalate", {"owner": "keel-servicing", "summary": "{summary}"}),
                    _submit("quarantine_and_escalate", "source:payments"),
                ),
            )
        )
    return out


def rename() -> list[TaskSpec]:
    cases = [
        ("a", "payments", "2026-11-10", "2026-11-10", "amount", "payment_amount", None, None),
        (
            "b",
            "bureau",
            "2026-11-02",
            "2026-11-02",
            "score",
            "fico_score",
            Announce(
                d("2026-10-28"),
                "Meridian Credit Bureau",
                "From the 2026-11-02 refresh the score column is delivered as fico_score.",
            ),
            None,
        ),
        ("c", "payments", "2026-10-07", "2026-10-06", "loan_id", "loan_number", None, "2026-10-06"),
    ]
    out = []
    for v, src, ds, ws, old, new, notice, noise in cases:
        alerts = {f"schema_{src}", "task_failures", f"freshness_raw_{src}", "freshness_mart_delinquency"}
        faults: tuple[Any, ...] = (Rename(src, d(ds), old, new),) + ((notice,) if notice else ())
        benign: tuple[Any, ...] = (CdcReplay(d(noise), 0.5),) if noise else ()
        if noise:
            alerts.add("volume_raw_payments")
        out.append(
            TaskSpec(
                id=f"rename_{v}",
                family="rename",
                variant=v,
                summary=f"{src} file for {ds} renames {old} to {new}"
                + (" with 5 days' notice" if notice else " without notice"),
                incident_ds=d(ds),
                window_start=d(ws),
                decision="escalate",
                root_cause=f"source:{src}",
                expected_alerts=frozenset(alerts),
                reference_mode="initial",
                faults=faults,
                benign=benign,
                acceptable=(Rule("escalate", {"owner": [OWNER[src]]}),),
                required_escalation=OWNER[src],
                oracle=(
                    ("escalate", {"owner": OWNER[src], "summary": "{summary}"}),
                    _submit("escalate", f"source:{src}"),
                ),
                noise="cdc_replay" if noise else None,
            )
        )
    return out


def discontinuation() -> list[TaskSpec]:
    cases = [
        (
            "a",
            ("HELOC",),
            "2026-11-09",
            "2026-10-14",
            "Home equity lines of credit (HELOC) close to new applications. The last HELOC originations "
            "are booked on 2026-11-06; no HELOC originations from 2026-11-09.",
        ),
        (
            "b",
            ("AUTO60", "PERS36"),
            "2026-10-20",
            "2026-09-30",
            "Harbor Lending exits auto and personal lending. AUTO60 and PERS36 are not originated from "
            "2026-10-20; existing loans are serviced to maturity.",
        ),
        (
            "c",
            ("HELOC", "PERS36"),
            "2026-09-21",
            "2026-09-02",
            "HELOC and PERS36 products are withdrawn from 2026-09-21 while the consumer product range is "
            "reviewed. No originations of either from that date.",
        ),
    ]
    out = []
    for v, products, eff, ann, text in cases:
        out.append(
            TaskSpec(
                id=f"discontinuation_{v}",
                family="discontinuation",
                variant=v,
                summary=f"{'+'.join(products)} discontinued from {eff}, announced {ann}",
                incident_ds=d(eff),
                window_start=d(eff),
                decision="no_action",
                root_cause="source:calendar",
                root_cause_alternates=("source:loans",),
                expected_alerts=frozenset({"volume_raw_loans"}),
                reference_mode="initial",
                benign=(Discontinue(products, d(eff), d(ann), text),),
                acceptable=(
                    Rule(
                        "update_monitor",
                        {"name": ["volume_raw_loans"], "params": {"baseline_start": _days(d(eff), d(eff))}},
                    ),
                ),
                allowed_monitor_changes=("volume_raw_loans",),
                oracle=(CLOSE_ALL, _submit("no_action", "source:calendar")),
            )
        )
    return out


def tight_config() -> list[TaskSpec]:
    cases = [
        (
            "a",
            "volume_raw_payments",
            "threshold",
            0.05,
            0.35,
            (0.2, 0.6),
            "2026-11-04",
            "2026-11-04",
            at("2026-11-04 14:05:00"),
            "sam",
            "Tighten payments volume alerting\n\nCatch partial files earlier.",
        ),
        (
            "b",
            "freshness_raw_payments",
            "max_partition_lag_days",
            0,
            1,
            (1, 2),
            "2026-10-21",
            "2026-10-21",
            at("2026-10-21 17:30:00"),
            "sam",
            "freshness: payments are due the same day (SLA review)",
        ),
        (
            "c",
            "volume_raw_loans",
            "threshold",
            0.06,
            0.30,
            (0.2, 0.6),
            "2026-10-28",
            "2026-10-28",
            at("2026-10-27 16:00:00"),
            "tom",
            "Tighter origination volume monitor",
        ),
    ]
    out = []
    for v, mon, param, bad, good, rng_, ds, ws, when, who, msg in cases:
        out.append(
            TaskSpec(
                id=f"tight_config_{v}",
                family="tight_config",
                variant=v,
                summary=f"{mon}.{param} changed from {good} to {bad}",
                incident_ds=d(ds),
                window_start=d(ws),
                decision="fix_config",
                root_cause=f"monitor:{mon}",
                expected_alerts=frozenset({mon}),
                reference_mode="initial",
                faults=(ConfigCommit(mon, param, bad, when, who, msg),),
                acceptable=(
                    Rule("update_monitor", {"name": [mon], "params": {param: rng_}}),
                    Rule("revert_commit", {"sha": ["{config_sha}"]}),
                ),
                monitor_requirements=(MonitorRequirement(mon, param, rng_[0], rng_[1]),),
                allowed_monitor_changes=(mon,),
                oracle=(
                    ("update_monitor", {"name": mon, "params": {param: good}}),
                    CLOSE_ALL,
                    _submit("fix_config", f"monitor:{mon}"),
                ),
            )
        )
    return out


def cdc_replay() -> list[TaskSpec]:
    out = []
    for v, ds, frac in [("a", "2026-11-03", 1.0), ("b", "2026-10-29", 0.45), ("c", "2026-09-30", 0.8)]:
        out.append(
            TaskSpec(
                id=f"cdc_replay_{v}",
                family="cdc_replay",
                variant=v,
                summary=f"{frac:.0%} of {ds} payment events re-sent; staging deduplicates",
                incident_ds=d(ds),
                window_start=d(ds),
                decision="no_action",
                root_cause="source:payments",
                expected_alerts=frozenset({"volume_raw_payments"}),
                reference_mode="initial",
                benign=(CdcReplay(d(ds), frac),),
                oracle=(CLOSE_ALL, _submit("no_action", "source:payments")),
            )
        )
    return out


def stale_reference() -> list[TaskSpec]:
    cases = [
        ("a", "2026-11-02", "2026-11-03 10:15:00", "2026-11-04", None),
        ("b", "2026-10-19", "2026-10-20 09:40:00", "2026-10-21", None),
        ("c", "2026-10-05", "2026-10-06 11:20:00", "2026-10-08", "2026-10-07"),
    ]
    out = []
    for v, monday, arrival, ds, noise in cases:
        parts = _days(d(monday), d(ds))
        affected = parts[1:]
        alerts = {"dbt_tests"}
        benign: tuple[Any, ...] = ()
        if noise:
            benign = (CdcReplay(d(noise), 0.5),)
            alerts.add("volume_raw_payments")
        out.append(
            TaskSpec(
                id=f"stale_reference_{v}",
                family="stale_reference",
                variant=v,
                summary=f"branch snapshot for {monday} arrived after the weekly load; new branch unmapped",
                incident_ds=d(ds),
                window_start=d(monday),
                decision="backfill",
                root_cause="source:branches",
                expected_alerts=frozenset(alerts),
                reference_mode="clean",
                faults=(LateFile("branches", d(monday), at(arrival)),),
                benign=benign,
                acceptable=(
                    Rule("rerun_task", {"task": ["ingest_branches", "dbt_build", "publish"], "partition": parts}),
                    Rule(
                        "backfill",
                        {
                            "target": ["all", "dbt_build", "ingest_branches", "dim_branch", "dim_branch+"],
                            "partitions": parts,
                        },
                    ),
                ),
                affected_partitions=tuple(affected),
                oracle=(
                    ("rerun_task", {"task": "ingest_branches", "partition": monday}),
                    ("backfill", {"target": "dbt_build", "partitions": affected}),
                    CLOSE_ALL,
                    _submit("backfill", "source:branches"),
                ),
                noise="cdc_replay" if noise else None,
            )
        )
    return out


def truncated() -> list[TaskSpec]:
    cases = [
        ("a", "payments", "2026-11-09", 0.6, "2026-11-10 07:50:31", None),
        ("b", "payments", "2026-10-27", 0.55, None, None),
        ("c", "loans", "2026-10-01", 0.5, "2026-10-02 07:35:12", "2026-10-01"),
    ]
    out = []
    for v, src, ds, keep, redelivery, noise in cases:
        T = d(ds)
        alerts = {"task_failures", "freshness_mart_delinquency"}
        if src == "payments":
            alerts.add("freshness_raw_payments")
        benign: tuple[Any, ...] = ()
        if noise:
            benign = (CdcReplay(d(noise), 0.5),)
            alerts.add("volume_raw_payments")
        fault = Truncate(src, T, keep, at(redelivery) if redelivery else None)
        if redelivery:
            spec = TaskSpec(
                id=f"truncated_{v}",
                family="truncated",
                variant=v,
                summary=f"{src} file for {ds} truncated; complete re-delivery is in landing",
                incident_ds=T,
                window_start=T,
                decision="rerun",
                root_cause=f"source:{src}",
                expected_alerts=frozenset(alerts),
                reference_mode="clean",
                faults=(fault,),
                benign=benign,
                acceptable=(
                    Rule("rerun_task", {"task": [f"ingest_{src}", "dbt_build", "publish"], "partition": [ds]}),
                    Rule("backfill", {"target": ["all", f"ingest_{src}"], "partitions": [ds]}),
                ),
                affected_partitions=(ds,),
                oracle=(
                    ("rerun_task", {"task": f"ingest_{src}", "partition": ds}),
                    CLOSE_ALL,
                    _submit("rerun", f"source:{src}"),
                ),
                noise="cdc_replay" if noise else None,
            )
        else:
            spec = TaskSpec(
                id=f"truncated_{v}",
                family="truncated",
                variant=v,
                summary=f"{src} file for {ds} truncated; no re-delivery",
                incident_ds=T,
                window_start=T,
                decision="escalate",
                root_cause=f"source:{src}",
                expected_alerts=frozenset(alerts),
                reference_mode="initial",
                faults=(fault,),
                benign=benign,
                acceptable=(Rule("escalate", {"owner": [OWNER[src]]}),),
                required_escalation=OWNER[src],
                oracle=(
                    ("escalate", {"owner": OWNER[src], "summary": "{summary}"}),
                    _submit("escalate", f"source:{src}"),
                ),
            )
        out.append(spec)
    return out


@cache
def all_tasks() -> tuple[TaskSpec, ...]:
    tasks: list[TaskSpec] = []
    for fn in (
        late_file,
        holiday,
        transient,
        fanout,
        cents,
        rename,
        discontinuation,
        tight_config,
        cdc_replay,
        stale_reference,
        truncated,
    ):
        tasks.extend(fn())
    ids = [t.id for t in tasks]
    assert len(ids) == len(set(ids))
    return tuple(tasks)


def get(task_id: str) -> TaskSpec:
    for t in all_tasks():
        if t.id == task_id:
            return t
    raise KeyError(f"unknown task {task_id!r}; see `triagebench list`")
