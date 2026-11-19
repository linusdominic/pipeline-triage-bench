"""Checks that the benchmark itself is sound.

1. oracle: the scripted correct solution resolves every task.
2. determinism: generating the same seed again gives identical worlds and ground truth.
3. leakage: no read tool returns ground-truth tokens, fault markers or host paths.
4. alerts: on a fresh episode copy, the monitors fire exactly the alerts each task expects.
5. incremental: rebuilding every partition at once (full refresh) reproduces the
   partition-by-partition history of the base world.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from triagebench import fingerprint, runner
from triagebench.config import DEFAULT_SEED, cache_root
from triagebench.episode import Episode
from triagebench.tasks import build, catalog
from triagebench.tasks.spec import TaskSpec
from triagebench.world import dbt_exec, monitors, repo
from triagebench.world.world import World

FAULT_MARKERS = [
    r"triagebench",
    r"ground[_ ]truth",
    r"\boracle\b",
    r"\binject",
    r"\bfault\b",
    r"reference world",
    r"expected_alerts",
    r"\bnoise\b",
    r"\bno_action\b",
    r"\bfix_code\b",
    r"\bfix_config\b",
    r"quarantine_and_escalate",
    r"/Users/",
    r"/home/",
    r"seed-\d+",
    r"\.cache/",
]


def world_digest(world: World) -> dict[str, str]:
    def h(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""

    fp = fingerprint.world_fingerprint(world)
    return {
        "tables": fingerprint.digest(fp["tables"]),
        "landing": fingerprint.digest(fp["landing"]),
        "monitors": fingerprint.digest(fp["monitors"]),
        "repo_head": repo.head(world.repo),
        "task_runs": h(world.task_runs_file),
        "alerts": h(world.alerts_file),
        "announcements": h(world.reference / "announcements.md"),
        "state": h(world.state_file),
    }


def generation_digest(seed: int) -> dict[str, Any]:
    out: dict[str, Any] = {"base": world_digest(World(build.base_dir(seed) / "world"))}
    for t in catalog.all_tasks():
        gt = build.load_ground_truth(seed, t.id)
        out[t.id] = {
            "world": world_digest(World(build.task_dir(seed, t.id) / "world")),
            "reference": fingerprint.digest(gt["reference_tables"]),
            "placeholders": gt["placeholders"],
        }
    return out


def check_determinism(seed: int, jobs: int, log: Any = print) -> dict[str, Any]:
    first = generation_digest(seed)
    other = cache_root().parent / "triagebench-determinism"
    if other.exists():
        shutil.rmtree(other)
    old = os.environ.get("TRIAGEBENCH_CACHE")
    os.environ["TRIAGEBENCH_CACHE"] = str(other)
    try:
        t0 = time.time()
        build.generate(seed, jobs=jobs, log=lambda s: None)
        log(f"  second generation took {time.time() - t0:.0f}s")
        second = generation_digest(seed)
    finally:
        if old is None:
            os.environ.pop("TRIAGEBENCH_CACHE", None)
        else:
            os.environ["TRIAGEBENCH_CACHE"] = old
        shutil.rmtree(other, ignore_errors=True)
    diffs = [k for k in first if first[k] != second.get(k)]
    return {"ok": not diffs, "differences": diffs, "worlds_compared": len(first)}


def crawl(ep: Episode, spec: TaskSpec) -> str:
    """Call every read tool over everything it can reach; return all outputs concatenated."""
    api = ep.api
    out: list[str] = []

    def c(tool: str, **args: Any) -> Any:
        res = api.call(tool, args)
        out.append(json.dumps(res))
        return res.get("result")

    c("get_overview")
    for a in c("list_alerts", status="all") or []:
        c("get_alert", alert_id=a["id"])
    for sql in (
        "select * from duckdb_databases()",
        "select * from duckdb_settings()",
        "select current_setting('temp_directory')",
        "select * from duckdb_extensions()",
    ):
        c("query_warehouse", sql=sql, max_rows=500)
    for t in c("list_tables") or []:
        c("query_warehouse", sql=f"select * from {t['table']} order by all desc limit 40", max_rows=40)
    for f in c("list_landing_files") or []:
        info = c("read_landing_file", path=f["path"], max_lines=30)
        if info and info["total_lines"] > 30:
            c("read_landing_file", path=f["path"], start_line=info["total_lines"] - 5, max_lines=10)
    window = spec.window_start.toordinal() - 7
    runs = [r for r in ep.world.task_runs() if date.fromisoformat(r["ds"]).toordinal() >= window]
    c("list_task_runs", limit=200)
    for r in runs:
        c("read_task_log", run_id=r["run_id"])
    for commit in c("git_log", limit=100) or []:
        c("git_show", rev=commit["sha"])
    for f in c("list_repo_files") or []:
        c("read_repo_file", path=f)
    for node in (
        "raw.payments",
        "raw.loans",
        "raw.bureau",
        "raw.branches",
        "stg_payments",
        "fct_payments",
        "fct_loan_balance_daily",
        "mart_delinquency",
        "dim_branch",
    ):
        c("get_lineage", node=node)
    c("get_monitors")
    for name in (c("get_runbook") or {}).get("runbooks", []):
        c("get_runbook", name=name)
    c("get_owners")
    for src in ("loans", "payments", "bureau", "branches"):
        c("get_contract", source=src)
    c("get_calendar")
    c("get_announcements")
    return "\n".join(out)


def leak_tokens(spec: TaskSpec) -> list[str]:
    # Family ids that are ordinary words (holiday, transient, truncated, cents, rename,
    # discontinuation) legitimately appear in calendars, logs and runbooks; only the
    # identifier-like ones are markers.
    family = [rf"\b{re.escape(spec.family)}\b"] if "_" in spec.family or spec.family == "fanout" else []
    toks = [
        re.escape(spec.id),
        re.escape(spec.summary),
        *family,
        re.escape(catalog.FAMILIES[spec.family]),
        *FAULT_MARKERS,
        re.escape(str(cache_root())),
    ]
    return toks


def check_leakage(seed: int, log: Any = print) -> dict[str, Any]:
    problems: dict[str, list[str]] = {}
    chars = 0
    for spec in catalog.all_tasks():
        with Episode(spec.id, seed) as ep:
            text = crawl(ep, spec)
            chars += len(text)
            hits = [tok for tok in leak_tokens(spec) if re.search(tok, text, flags=re.IGNORECASE)]
            # the tool descriptions are static and identical for every task
            desc = json.dumps(ep.api.specs())
            hits += [
                f"description:{tok}"
                for tok in (re.escape(spec.id), re.escape(str(cache_root())))
                if re.search(tok, desc)
            ]
            if hits:
                problems[spec.id] = hits
    return {"ok": not problems, "problems": problems, "characters_crawled": chars}


def check_alerts(seed: int) -> dict[str, Any]:
    problems = {}
    for spec in catalog.all_tasks():
        with Episode(spec.id, seed) as ep:
            fired = {f.monitor for f in monitors.evaluate(ep.world)}
            if fired != set(spec.expected_alerts):
                problems[spec.id] = {"expected": sorted(spec.expected_alerts), "fired": sorted(fired)}
    return {"ok": not problems, "problems": problems, "tasks": len(catalog.all_tasks())}


def check_incremental(seed: int) -> dict[str, Any]:
    base = World(build.base_dir(seed) / "world")
    tmp = cache_root() / "incremental-check"
    w = base.copy_to(tmp)
    try:
        before = fingerprint.tables(w)
        res = dbt_exec.run(w, "build", None, full_refresh=True)
        after = fingerprint.tables(w)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    diffs = [t for t in before if t.startswith("main.") and before[t] != after.get(t)]
    return {"ok": res.success and not diffs, "dbt_success": res.success, "differences": diffs}


def oracle(seed: int, jobs: int) -> dict[str, Any]:
    rows = runner.run_agents(["oracle"], [t.id for t in catalog.all_tasks()], seed, jobs, None, log=lambda s: None)[
        "oracle"
    ]
    bad = [r["task"] for r in rows if not r["resolved"]]
    return {"ok": not bad, "resolved": len(rows) - len(bad), "tasks": len(rows), "unresolved": bad}


def validate(seed: int = DEFAULT_SEED, jobs: int = 4, determinism: bool = True, log: Any = print) -> dict[str, Any]:
    report: dict[str, Any] = {"seed": seed}
    t0 = time.time()
    build.generate(seed, jobs=jobs, log=lambda s: None)
    steps: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("oracle", lambda: oracle(seed, jobs)),
        ("alerts", lambda: check_alerts(seed)),
        ("leakage", lambda: check_leakage(seed)),
        ("incremental", lambda: check_incremental(seed)),
    ]
    if determinism:
        steps.append(("determinism", lambda: check_determinism(seed, jobs, log)))
    for name, fn in steps:
        t = time.time()
        report[name] = fn()
        report[name]["seconds"] = round(time.time() - t, 1)
        log(
            f"{name:<12} {'PASS' if report[name]['ok'] else 'FAIL'}  ({report[name]['seconds']}s) "
            + json.dumps({k: v for k, v in report[name].items() if k not in ("ok", "seconds")})[:300]
        )
    report["ok"] = all(report[n]["ok"] for n, _ in steps)
    report["seconds"] = round(time.time() - t0, 1)
    return report
