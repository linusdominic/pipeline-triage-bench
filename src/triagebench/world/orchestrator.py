"""A small scheduler for the daily DAG, writing Airflow-like task-run records and logs.

Time is simulated: each attempt advances the world's clock by a duration derived from the
work done, so records are deterministic.
"""

from __future__ import annotations

import importlib.util
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from triagebench.config import BACKBOOK_DATE, BANK_HOLIDAYS
from triagebench.world import calendar as cal
from triagebench.world import dbt_exec
from triagebench.world.calendar import fmt_ts
from triagebench.world.world import World

DAG_ID = "harbor_daily"
SOURCES = ["loans", "payments", "bureau", "branches"]


@dataclass(frozen=True)
class TaskDef:
    name: str
    retries: int
    retry_delay_s: int
    upstream: tuple[str, ...] = ()


@dataclass(frozen=True)
class FailureHook:
    """Make the first `attempts` attempts of a task fail with the given log lines (generation only)."""

    attempts: int
    error: str
    detail: tuple[str, ...] = ()
    duration_s: int = 300


@dataclass
class Hooks:
    failures: dict[tuple[str, str], FailureHook] = field(default_factory=dict)


class DagError(Exception):
    pass


def dag_for(ds: date) -> list[TaskDef]:
    if ds == BACKBOOK_DATE:
        return [TaskDef("ingest_loans", 2, 600)]
    if not cal.is_business_day(ds):
        reason = BANK_HOLIDAYS.get(ds, "weekend")
        raise DagError(f"{DAG_ID} has no run for {ds.isoformat()} ({reason}); it runs for business days only")
    ingest = ["ingest_loans", "ingest_payments"]
    if cal.is_bureau_day(ds):
        ingest.append("ingest_bureau")
    if cal.is_first_business_day_of_week(ds):
        ingest.append("ingest_branches")
    tasks = [TaskDef(name, 2, 600) for name in ingest]
    tasks.append(TaskDef("dbt_build", 1, 300, tuple(ingest)))
    tasks.append(TaskDef("publish", 1, 120, ("dbt_build",)))
    return tasks


def downstream_of(tasks: list[TaskDef], name: str) -> list[str]:
    out: list[str] = []
    frontier = {name}
    for t in tasks:  # tasks are in topological order
        if frontier.intersection(t.upstream):
            out.append(t.name)
            frontier.add(t.name)
    return out


def _load_ingest_module(world: World, counter: str) -> ModuleType:
    path = world.repo / "ingestion" / "harbor_ingest.py"
    spec = importlib.util.spec_from_file_location(f"harbor_ingest_{counter}", path)
    if spec is None or spec.loader is None:
        raise DagError("cannot load ingestion/harbor_ingest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _format_exception(exc: BaseException, world: World) -> list[str]:
    lines = ["Traceback (most recent call last):"]
    for fr in traceback.extract_tb(exc.__traceback__):
        if fr.filename.startswith(str(world.repo)):
            rel = Path(fr.filename).relative_to(world.repo)
            lines.append(f'  File "{rel}", line {fr.lineno}, in {fr.name}')
            if fr.line:
                lines.append(f"    {fr.line}")
    lines.append(f"{type(exc).__name__}: {exc}")
    return lines


class Orchestrator:
    def __init__(self, world: World, hooks: Hooks | None = None) -> None:
        self.world = world
        self.hooks = hooks or Hooks()

    # --- records ---------------------------------------------------------------------
    def _latest_state(self, task: str, ds: date) -> str | None:
        state = None
        for r in self.world.task_runs():
            if r["task_id"] == task and r["ds"] == ds.isoformat():
                state = r["state"]
        return state

    def _next_attempt(self, task: str, ds: date) -> int:
        n = 0
        for r in self.world.task_runs():
            if r["task_id"] == task and r["ds"] == ds.isoformat():
                n = max(n, int(r["attempt"]))
        return n + 1

    def _record(
        self,
        task: str,
        ds: date,
        attempt: int,
        state: str,
        start: datetime,
        end: datetime,
        trigger: str,
        log_lines: list[str],
        error: str | None,
        note: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or self.world.next_run_id()
        log_rel = f"orchestrator/logs/ds={ds.isoformat()}/{task}/{run_id}.log"
        log_path = self.world.root / log_rel
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(log_lines) + "\n")
        rec: dict[str, Any] = {
            "run_id": run_id,
            "dag_id": DAG_ID,
            "task_id": task,
            "ds": ds.isoformat(),
            "attempt": attempt,
            "state": state,
            "start": fmt_ts(start),
            "end": fmt_ts(end),
            "duration_s": int((end - start).total_seconds()),
            "trigger": trigger,
            "error": error,
            "log": log_rel,
        }
        if note:
            rec["note"] = note
        self.world.append_task_run(rec)
        return rec

    # --- execution ---------------------------------------------------------------------
    def run_dag(self, ds: date, trigger: str = "scheduled", start: datetime | None = None) -> dict[str, str]:
        tasks = dag_for(ds)
        if start is not None:
            self.world.set_now(start)
        states: dict[str, str] = {}
        for t in tasks:
            if any(states.get(u) != "success" for u in t.upstream):
                now = self.world.now()
                self._record(
                    t.name,
                    ds,
                    0,
                    "upstream_failed",
                    now,
                    now,
                    trigger,
                    [f"[{fmt_ts(now)}] INFO - Upstream task failed; {t.name} not run"],
                    None,
                )
                states[t.name] = "upstream_failed"
                continue
            states[t.name] = self.run_task(t, ds, trigger)
        return states

    def run_task(self, t: TaskDef, ds: date, trigger: str, select: str | None = None) -> str:
        first = self._next_attempt(t.name, ds)
        for i in range(t.retries + 1):
            attempt = first + i
            ok = self._attempt(t, ds, attempt, i, t.retries + 1, trigger, select)
            if ok:
                return "success"
            if i < t.retries:
                self.world.advance(t.retry_delay_s)
        return "failed"

    def _attempt(
        self, t: TaskDef, ds: date, attempt: int, idx: int, total: int, trigger: str, select: str | None
    ) -> bool:
        start = self.world.now()
        log: list[str] = []
        elapsed = 0

        def emit(msg: str, level: str = "INFO") -> None:
            log.append(f"[{fmt_ts(start + timedelta(seconds=elapsed))}] {level} - {msg}")

        emit(f"Starting attempt {idx + 1} of {total} for {DAG_ID}.{t.name} ds={ds.isoformat()} (trigger: {trigger})")
        run_id = self.world.next_run_id()
        hook = self.hooks.failures.get((t.name, ds.isoformat()))
        if hook is not None and trigger == "scheduled" and idx < hook.attempts:
            elapsed = hook.duration_s
            for line in hook.detail:
                emit(line, "ERROR" if line.startswith(("Traceback", "  ")) else "INFO")
            emit(f"Task failed: {hook.error}", "ERROR")
            end = start + timedelta(seconds=elapsed)
            self.world.set_now(end)
            self._record(t.name, ds, attempt, "failed", start, end, trigger, log, hook.error, run_id=run_id)
            return False

        error: str | None = None
        try:
            if t.name.startswith("ingest_"):
                source = t.name.removeprefix("ingest_")
                mod = _load_ingest_module(self.world, f"{attempt}_{ds:%Y%m%d}")
                result = mod.run(
                    source,
                    ds,
                    landing_dir=self.world.landing,
                    contracts_dir=self.world.repo / "contracts",
                    warehouse=self.world.warehouse,
                    run_time=fmt_ts(start),
                    log=lambda m: emit(m),
                )
                elapsed = 6 + int(result.get("rows", 0)) // 50
            elif t.name == "dbt_build":
                elapsed, error = self._dbt_build(ds, emit, select, run_id)
            elif t.name == "publish":
                conn = self.world.connect()
                try:
                    conn.execute(
                        "insert into ops.published_partitions values (?, ?, ?)",
                        [ds, run_id, fmt_ts(start)],
                    )
                finally:
                    conn.close()
                emit(f"Published mart_delinquency partition {ds.isoformat()} to finance-reporting")
                elapsed = 6
        except Exception as exc:  # task code failed
            elapsed = max(elapsed, 4)
            for line in _format_exception(exc, self.world):
                emit(line, "ERROR")
            error = f"{type(exc).__name__}: {exc}"
        end = start + timedelta(seconds=max(elapsed, 1))
        self.world.set_now(end)
        if error is None:
            emit(f"Task succeeded in {int((end - start).total_seconds())}s")
            self._record(t.name, ds, attempt, "success", start, end, trigger, log, None, run_id=run_id)
            return True
        emit(f"Task failed: {error}", "ERROR")
        self._record(t.name, ds, attempt, "failed", start, end, trigger, log, error, run_id=run_id)
        return False

    def _dbt_build(
        self, ds: date | None, emit: Any, select: str | None, run_id: str, full_refresh: bool = False
    ) -> tuple[int, str | None]:
        cmd = (
            "dbt build"
            + (f" --select {select}" if select else "")
            + (f" --vars '{{ds: \"{ds.isoformat()}\"}}'" if ds else "")
            + (" --full-refresh" if full_refresh else "")
        )
        emit(f"Running: {cmd}")
        res = dbt_exec.run(self.world, "build", ds, select=select, full_refresh=full_refresh)
        if res.error:
            emit(res.error, "ERROR")
            return 20, f"dbt build failed: {res.error.splitlines()[0]}"
        n = len(res.nodes)
        counts = {"pass": 0, "warn": 0, "error": 0, "fail": 0, "skip": 0, "success": 0}
        rows: list[list[Any]] = []
        for i, node in enumerate(res.nodes, start=1):
            status = node.status.lower()
            counts[status] = counts.get(status, 0) + 1
            kind = node.resource_type.lower().split(".")[-1]
            label = {
                "success": "OK",
                "pass": "PASS",
                "warn": "WARN",
                "fail": "FAIL",
                "error": "ERROR",
                "skipped": "SKIP",
            }.get(status, status.upper())
            extra = f" ({node.message})" if status in ("fail", "warn", "error") and node.message else ""
            emit(
                f"{i} of {n} {label} {kind} {node.name}{extra}",
                "WARNING" if status == "warn" else ("ERROR" if status in ("fail", "error") else "INFO"),
            )
            if kind == "test":
                rows.append([run_id, ds, node.name, node.model, status, node.failures or 0, node.message])
        if rows and ds is not None:
            conn = self.world.connect()
            try:
                conn.executemany("insert into ops.dbt_test_results values (?, ?, ?, ?, ?, ?, ?)", rows)
            finally:
                conn.close()
        failed = counts.get("fail", 0) + counts.get("error", 0)
        skipped = counts.get("skipped", 0)
        emit(
            f"Done. PASS={counts.get('pass', 0) + counts.get('success', 0)} WARN={counts.get('warn', 0)} "
            f"ERROR={counts.get('error', 0)} FAIL={counts.get('fail', 0)} SKIP={skipped} TOTAL={n}"
        )
        elapsed = 30 + 2 * n
        if failed:
            return elapsed, f"dbt build finished with {failed} failing node(s) and {skipped} skipped"
        return elapsed, None

    def full_refresh(self, ds: date, note: str) -> str:
        start = self.world.now()
        log: list[str] = []

        def emit(msg: str, level: str = "INFO") -> None:
            log.append(f"[{fmt_ts(start)}] {level} - {msg}")

        run_id = self.world.next_run_id()
        emit(f"Starting {DAG_ID}.dbt_build ({note})")
        elapsed, error = self._dbt_build(None, emit, None, run_id, full_refresh=True)
        end = start + timedelta(seconds=elapsed * 4)
        self.world.set_now(end)
        state = "success" if error is None else "failed"
        self._record(
            "dbt_build",
            ds,
            self._next_attempt("dbt_build", ds),
            state,
            start,
            end,
            "backfill",
            log,
            error,
            note,
            run_id=run_id,
        )
        return state

    # --- manual operations ---------------------------------------------------------------
    def rerun(
        self, task: str, ds: date, include_downstream: bool = True, trigger: str = "manual_rerun"
    ) -> dict[str, str]:
        tasks = dag_for(ds)
        by_name = {t.name: t for t in tasks}
        if task not in by_name:
            raise DagError(f"task {task} is not part of {DAG_ID} for {ds.isoformat()}; tasks: {list(by_name)}")
        states = {task: self.run_task(by_name[task], ds, trigger)}
        if include_downstream:
            for name in downstream_of(tasks, task):
                t = by_name[name]
                if all((states.get(u) or self._latest_state(u, ds)) == "success" for u in t.upstream):
                    states[name] = self.run_task(t, ds, trigger)
                else:
                    now = self.world.now()
                    self._record(
                        name,
                        ds,
                        0,
                        "upstream_failed",
                        now,
                        now,
                        trigger,
                        [f"[{fmt_ts(now)}] INFO - Upstream task failed; {name} not run"],
                        None,
                    )
                    states[name] = "upstream_failed"
        return states

    def build_models(self, select: str, ds: date, trigger: str = "backfill") -> str:
        dag_for(ds)  # validates the partition
        t = TaskDef("dbt_build", 0, 0)
        return self.run_task(t, ds, trigger, select=select)
