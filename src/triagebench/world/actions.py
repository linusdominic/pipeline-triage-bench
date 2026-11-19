"""Write operations on a world. The agent tools and the task builder both use these."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import yaml

from triagebench.world import dbt_exec, monitors, repo
from triagebench.world.calendar import fmt_ts
from triagebench.world.orchestrator import DagError, Orchestrator, dag_for
from triagebench.world.world import World


class ActionError(Exception):
    pass


MONITOR_PARAMS: dict[str, dict[str, type | tuple[type, ...]]] = {
    "freshness": {"calendar": str, "max_partition_lag_days": int, "severity": str, "enabled": bool},
    "volume": {
        "threshold": (int, float),
        "baseline_partitions": int,
        "baseline_start": str,
        "lookback_partitions": int,
        "severity": str,
        "enabled": bool,
    },
    "schema": {"severity": str, "enabled": bool},
    "reconciliation": {"tolerance_pct": (int, float), "severity": str, "enabled": bool},
    "dbt_tests": {"severity": str, "enabled": bool},
    "task_failure": {"severity": str, "enabled": bool},
}


def _ds(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError) as exc:
        raise ActionError(f"partition must be a date like 2026-11-05, got {s!r}") from exc


def _changed_models(paths: list[str]) -> list[str]:
    out = []
    for p in paths:
        m = re.match(r"dbt/models/.+/([a-z0-9_]+)\.sql$", p)
        if m:
            out.append(m.group(1))
    return sorted(set(out))


def _rebuild(world: World, models: list[str], partitions: list[str]) -> dict[str, str]:
    orch = Orchestrator(world)
    results: dict[str, str] = {}
    if not models:
        return results
    select = " ".join(f"{m}+" for m in models)
    for p in sorted(partitions):
        results[p] = orch.build_models(select, _ds(p))
    return results


def rerun_task(world: World, task: str, partition: str, include_downstream: bool = True) -> dict[str, Any]:
    try:
        states = Orchestrator(world).rerun(task, _ds(partition), include_downstream, trigger="manual_rerun")
    except DagError as exc:
        raise ActionError(str(exc)) from exc
    monitors.run(world)
    return {"partition": partition, "states": states}


def backfill(world: World, target: str, partitions: list[str]) -> dict[str, Any]:
    if not partitions:
        raise ActionError("partitions must list at least one partition")
    orch = Orchestrator(world)
    results: dict[str, Any] = {}
    model_names = {p.stem for p in (world.dbt_dir / "models").rglob("*.sql")}
    for p in sorted(partitions):
        ds = _ds(p)
        try:
            tasks = [t.name for t in dag_for(ds)]
            if target == "all":
                results[p] = orch.run_dag(ds, trigger="backfill")
            elif target in tasks:
                results[p] = orch.rerun(target, ds, True, trigger="backfill")
            elif target.rstrip("+") in model_names:
                results[p] = {"dbt_build": orch.build_models(target, ds)}
            else:
                raise ActionError(
                    f"unknown target {target!r}: use 'all', a task name ({', '.join(tasks)}) or a dbt model "
                    f"name, optionally with '+' for its downstream models"
                )
        except DagError as exc:
            results[p] = {"error": str(exc)}
    monitors.run(world)
    return {"target": target, "results": results}


def apply_patch(world: World, diff: str, message: str, partitions: list[str] | None = None) -> dict[str, Any]:
    if not diff.strip():
        raise ActionError("empty diff")
    if not diff.endswith("\n"):
        diff += "\n"
    when = world.now()
    try:
        repo.git(world.repo, "apply", "--check", "-", stdin=diff)
    except repo.GitError as exc:
        raise ActionError(f"patch does not apply: {exc}") from exc
    numstat = repo.git(world.repo, "apply", "--numstat", "-", stdin=diff)
    paths = [line.split("\t")[2] for line in numstat.splitlines() if line.count("\t") >= 2]
    repo.git(world.repo, "apply", "-", stdin=diff)
    sha = repo.commit_all(world.repo, message or "on-call fix", when, "oncall")
    if any(p.startswith("dbt/") for p in paths):
        res = dbt_exec.run(world, "parse", None)
        if res.error:
            repo.git(world.repo, "reset", "--hard", "HEAD~1")
            raise ActionError(f"dbt parse failed after the patch, commit undone: {res.error}")
    world.advance(30)
    rebuilt = _rebuild(world, _changed_models(paths), partitions or [])
    monitors.run(world)
    return {"commit": sha, "files": paths, "rebuilt": rebuilt}


def revert_commit(world: World, sha: str, partitions: list[str] | None = None) -> dict[str, Any]:
    try:
        full = repo.git(world.repo, "rev-parse", "--verify", f"{sha}^{{commit}}").strip()
        paths = [p for p in repo.git(world.repo, "show", "--name-only", "--format=", full).splitlines() if p]
        repo.git(world.repo, "revert", "--no-edit", full, when=world.now(), author="oncall")
    except repo.GitError as exc:
        raise ActionError(f"cannot revert {sha}: {exc}") from exc
    new = repo.head(world.repo)
    world.advance(30)
    rebuilt = _rebuild(world, _changed_models(paths), partitions or [])
    monitors.run(world)
    return {"reverted": full, "commit": new, "files": paths, "rebuilt": rebuilt}


def set_monitor_params(text: str, name: str, params: dict[str, Any]) -> str:
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^\s*- name: {re.escape(name)}\s*$", ln)), None)
    if start is None:
        raise ActionError(f"no monitor named {name!r}")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*- name: ", lines[i])), len(lines))
    indent = re.match(r"^(\s*)- ", lines[start])
    pad = " " * (len(indent.group(1)) + 2) if indent else "    "
    for key, value in params.items():
        rendered = yaml.safe_dump(value, default_flow_style=True).strip()
        rendered = rendered.removesuffix("...").strip()
        done = False
        for i in range(start + 1, end):
            m = re.match(rf"^(\s+{re.escape(key)}:\s*)([^#\n]*?)(\s+#.*)?(\n?)$", lines[i])
            if m:
                lines[i] = f"{m.group(1)}{rendered}{m.group(3) or ''}{m.group(4)}"
                done = True
                break
        if not done:
            last = end - 1
            while last > start and not lines[last].strip():
                last -= 1
            lines.insert(last + 1, f"{pad}{key}: {rendered}\n")
            end += 1
    return "".join(lines)


def update_monitor(
    world: World, name: str, params: dict[str, Any], message: str | None = None, author: str = "oncall"
) -> dict[str, Any]:
    cfg = monitors.load_config(world)
    mon = next((m for m in cfg.get("monitors", []) if m.get("name") == name), None)
    if mon is None:
        raise ActionError(f"no monitor named {name!r}")
    allowed = MONITOR_PARAMS.get(str(mon.get("type")), {})
    if not params:
        raise ActionError("params must set at least one parameter")
    for k, v in params.items():
        if k not in allowed:
            raise ActionError(f"{name} ({mon.get('type')}) has no parameter {k!r}; allowed: {sorted(allowed)}")
        if not isinstance(v, allowed[k]) or (isinstance(v, bool) and allowed[k] is not bool):
            raise ActionError(f"{k} must be of type {allowed[k]}")
        if k == "calendar" and v not in ("weekdays", "business_calendar"):
            raise ActionError("calendar must be 'weekdays' or 'business_calendar'")
        if k == "baseline_start":
            _ds(str(v))
    path = world.repo / "monitoring" / "monitors.yml"
    path.write_text(set_monitor_params(path.read_text(), name, params))
    summary = ", ".join(f"{k}={json.dumps(v)}" for k, v in params.items())
    sha = repo.commit_all(world.repo, message or f"monitoring: {name}: {summary}", world.now(), author)
    world.advance(10)
    alerts = monitors.run(world)
    return {
        "monitor": name,
        "params": params,
        "commit": sha,
        "open_alerts": sum(1 for a in alerts if a["status"] == "open"),
    }


def quarantine(world: World, table: str, partition: str, reason: str) -> dict[str, Any]:
    if table not in ("raw.loans", "raw.payments", "raw.bureau"):
        raise ActionError("quarantine works on raw source partitions: raw.loans, raw.payments or raw.bureau")
    ds = _ds(partition)
    conn = world.connect()
    try:
        n = conn.execute(f"select count(*) from {table} where _ds = ?", [ds]).fetchone()
        if not n or n[0] == 0:
            raise ActionError(f"{table} has no partition {partition}")
        exists = conn.execute(
            "select count(*) from ops.quarantine where source_table = ? and ds = ?", [table, ds]
        ).fetchone()
        if exists and exists[0]:
            raise ActionError(f"{table} partition {partition} is already quarantined")
        conn.execute("insert into ops.quarantine values (?, ?, ?, ?)", [table, ds, reason, fmt_ts(world.now())])
    finally:
        conn.close()
    states = Orchestrator(world).rerun("dbt_build", ds, True, trigger="quarantine")
    monitors.run(world)
    return {"quarantined": f"{table} {partition}", "rebuild": states}


def owners(world: World) -> list[dict[str, Any]]:
    data: dict[str, Any] = yaml.safe_load((world.repo / "owners.yaml").read_text())
    return list(data.get("owners", []))


def escalate(world: World, owner: str, summary: str) -> dict[str, Any]:
    known = {o["id"]: o for o in owners(world)}
    if owner not in known:
        raise ActionError(f"unknown owner {owner!r}; owners: {sorted(known)}")
    if not summary.strip():
        raise ActionError("summary must say what is wrong and what you need from the owner")
    rec = {"at": fmt_ts(world.now()), "owner": owner, "contact": known[owner].get("contact"), "summary": summary}
    with open(world.outbox, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    world.advance(60)
    return {"sent_to": known[owner].get("contact"), "owner": owner}


def close_alert(world: World, alert_id: str, resolution: str, summary: str) -> dict[str, Any]:
    alerts = world.alerts()
    for a in alerts:
        if a["id"] == alert_id:
            a["status"] = "closed"
            a["closed_at"] = fmt_ts(world.now())
            a["resolution"] = resolution
            a["resolution_summary"] = summary
            world.save_alerts(alerts)
            return {"closed": alert_id}
    raise ActionError(f"no alert {alert_id!r}")


def run_write_sql(world: World, sql: str) -> dict[str, Any]:
    import duckdb

    conn = duckdb.connect(str(world.warehouse), config={"enable_external_access": False})
    try:
        cur = conn.execute(sql)
        try:
            rows = cur.fetchall()
        except duckdb.Error:
            rows = []
    except duckdb.Error as exc:
        raise ActionError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()
    world.advance(5)
    return {"result": [list(map(str, r)) for r in rows[:20]]}


def landing_path(world: World, path: str) -> Any:
    rel = path.removeprefix("landing/")
    p = (world.landing / rel).resolve()
    if not str(p).startswith(str(world.landing) + "/"):
        raise ActionError("path must be inside the landing zone")
    return p


def write_landing_file(world: World, path: str, content: str) -> dict[str, Any]:
    p = landing_path(world, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    world.advance(5)
    return {"written": str(p.relative_to(world.root)), "bytes": len(content.encode())}


def delete_landing_file(world: World, path: str) -> dict[str, Any]:
    p = landing_path(world, path)
    if not p.exists():
        raise ActionError(f"no such file: {path}")
    p.unlink()
    world.advance(5)
    return {"deleted": str(p.relative_to(world.root))}
