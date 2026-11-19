"""Monitors evaluated on a world's current state, and the alert lifecycle.

A monitor produces firing conditions, each identified by a key. A new key opens an alert;
a key that keeps firing updates it; an open alert whose key no longer fires is resolved.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from triagebench.world import calendar as cal
from triagebench.world.calendar import fmt_ts
from triagebench.world.world import World


@dataclass
class Firing:
    monitor: str
    kind: str
    severity: str
    key: str
    target: str
    partition: str | None
    message: str
    details: dict[str, Any]


def load_config(world: World) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((world.repo / "monitoring" / "monitors.yml").read_text())
    return data


def alert_id(key: str) -> str:
    return "ALR-" + hashlib.sha256(key.encode()).hexdigest()[:6].upper()


def _prev_on(calendar: str, d: date) -> date:
    return cal.prev_business_day(d) if calendar == "business_calendar" else cal.prev_weekday(d)


def _expected_daily(calendar: str, check: date, lag: int) -> date:
    exp = check
    for _ in range(max(lag, 0)):
        exp = _prev_on(calendar, exp)
    return exp


def _expected_weekly(calendar: str, now: datetime) -> date:
    """Newest weekly partition that should have been processed by `now`."""
    d = now.date() - timedelta(days=1)
    while True:
        if calendar == "business_calendar":
            if cal.is_bureau_day(d) and cal.run_time_for(d) <= now:
                return d
        elif d.weekday() == 0 and cal.run_time_for(d) <= now:
            return d
        d -= timedelta(days=1)


def _partitions(conn: Any, table: str, col: str) -> list[tuple[date, int]]:
    rows = conn.execute(f"select {col}, count(*) from {table} group by 1 order by 1").fetchall()
    return [(r[0], int(r[1])) for r in rows]


def _freshness(m: dict[str, Any], world: World, conn: Any, now: datetime) -> list[Firing]:
    table, col = m["table"], m.get("partition_column", "ds")
    calendar = m.get("calendar", "weekdays")
    row = conn.execute(f"select max({col}) from {table}").fetchone()
    latest: date | None = row[0] if row else None
    if m.get("schedule") == "weekly":
        expected = _expected_weekly(calendar, now)
        rule = f"weekly, calendar: {calendar}"
    else:
        lag = int(m.get("max_partition_lag_days", 1))
        expected = _expected_daily(calendar, now.date(), lag)
        rule = f"calendar: {calendar}, max lag {lag} day(s)"
    if latest is not None and latest >= expected:
        return []
    return [
        Firing(
            m["name"],
            "freshness",
            m.get("severity", "high"),
            f"{m['name']}|{expected}",
            table,
            expected.isoformat(),
            f"{table}: newest partition is {latest}, expected {expected.isoformat()} ({rule})",
            {"newest_partition": str(latest), "expected_partition": expected.isoformat(), "calendar": calendar},
        )
    ]


def _volume(m: dict[str, Any], world: World, conn: Any, now: datetime, lookback: int) -> list[Firing]:
    table, col = m["table"], m.get("partition_column", "ds")
    parts = _partitions(conn, table, col)
    base_n = int(m.get("baseline_partitions", 20))
    thr = float(m.get("threshold", 0.35))
    start = m.get("baseline_start")
    start_d = date.fromisoformat(str(start)) if start else None
    out: list[Firing] = []
    for i in range(max(0, len(parts) - lookback), len(parts)):
        p, n = parts[i]
        prior = [c for (d, c) in parts[max(0, i - base_n) : i] if start_d is None or d >= start_d]
        if len(prior) < 5:
            continue
        med = statistics.median(prior)
        if med <= 0:
            continue
        dev = n / med - 1
        if abs(dev) > thr:
            out.append(
                Firing(
                    m["name"],
                    "volume",
                    m.get("severity", "medium"),
                    f"{m['name']}|{p}",
                    table,
                    p.isoformat(),
                    f"{table} partition {p}: {n} rows, {dev:+.0%} against the median of the previous "
                    f"{len(prior)} partitions ({med:g}); threshold {thr:.0%}",
                    {"rows": n, "baseline_median": med, "relative_change": round(dev, 4), "threshold": thr},
                )
            )
    return out


def _newest_landing_file(world: World, contract: dict[str, Any]) -> Path | None:
    import re

    directory = world.landing / Path(contract["delivery"]["directory"]).name
    if not directory.is_dir():
        return None
    best: tuple[str, int, Path] | None = None
    for p in directory.iterdir():
        m = re.search(r"_(\d{8})(?:_r(\d+))?\.csv$", p.name)
        if not m:
            continue
        k = (m.group(1), int(m.group(2) or 0), p)
        if best is None or k[:2] > best[:2]:
            best = k
    return best[2] if best else None


def _contract(world: World, source: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((world.repo / "contracts" / f"{source}.yml").read_text())
    return data


def _schema(m: dict[str, Any], world: World) -> list[Firing]:
    contract = _contract(world, m["source"])
    path = _newest_landing_file(world, contract)
    if path is None:
        return []
    header = path.read_text().split("\n", 1)[0].strip().split(",")
    expected = [c["name"] for c in contract["columns"]]
    missing = [c for c in expected if c not in header]
    unexpected = [c for c in header if c not in expected]
    if not missing and not unexpected:
        return []
    rel = f"{contract['delivery']['directory']}/{path.name}"
    return [
        Firing(
            m["name"],
            "schema",
            m.get("severity", "high"),
            f"{m['name']}|{path.name}",
            f"source:{m['source']}",
            None,
            f"{rel} does not match contract {contract['contract']} v{contract['version']}: "
            f"missing {missing}, unexpected {unexpected}",
            {"file": rel, "missing_columns": missing, "unexpected_columns": unexpected},
        )
    ]


def _reconciliation(m: dict[str, Any], world: World, conn: Any, lookback: int) -> list[Firing]:
    contract = _contract(world, m["source"])
    directory = world.landing / Path(contract["delivery"]["directory"]).name
    table, amount = m["table"], m.get("amount_column", "amount")
    tol = float(m.get("tolerance_pct", 0.5))
    rows = conn.execute(
        f"select _ds, any_value(_file), count(*), sum({amount}) from {table} group by 1 order by 1 desc limit ?",
        [lookback],
    ).fetchall()
    out: list[Firing] = []
    for ds, fname, n, total in rows:
        path = directory / str(fname)
        if not path.exists():
            continue
        last = path.read_text().strip().splitlines()[-1]
        if not last.startswith("TRL|"):
            continue
        _, tcount, ttotal = last.split("|")
        t_total = float(ttotal)
        diff_pct = abs(float(total) - t_total) / abs(t_total) * 100 if t_total else 0.0
        if int(tcount) != int(n) or diff_pct > tol:
            out.append(
                Firing(
                    m["name"],
                    "reconciliation",
                    m.get("severity", "high"),
                    f"{m['name']}|{ds}",
                    table,
                    ds.isoformat(),
                    f"{table} partition {ds}: loaded {n} records totalling {float(total):,.2f}; trailer of {fname} "
                    f"says {tcount} records totalling {t_total:,.2f} ({diff_pct:.1f}% apart, tolerance {tol}%)",
                    {
                        "loaded_records": int(n),
                        "loaded_total": float(total),
                        "trailer_records": int(tcount),
                        "trailer_total": t_total,
                        "file": str(fname),
                    },
                )
            )
    return out


def _dbt_tests(m: dict[str, Any], conn: Any, lookback: int) -> list[Firing]:
    latest = conn.execute(
        """
        select ds, max(run_id) from ops.dbt_test_results
        group by ds order by ds desc limit ?
        """,
        [lookback],
    ).fetchall()
    failing: dict[str, dict[str, Any]] = {}
    for ds, run_id in latest:
        for name, model, status, failures, _message in conn.execute(
            "select test_name, model, status, failures, message from ops.dbt_test_results "
            "where run_id = ? and status in ('fail', 'error', 'warn') order by test_name",
            [run_id],
        ).fetchall():
            f = failing.setdefault(name, {"model": model, "status": status, "partitions": [], "failures": {}})
            f["partitions"].append(ds.isoformat())
            f["failures"][ds.isoformat()] = failures
            if status in ("fail", "error"):
                f["status"] = status
    out: list[Firing] = []
    for name, f in sorted(failing.items()):
        parts = sorted(f["partitions"])
        sev = "low" if f["status"] == "warn" else m.get("severity", "high")
        out.append(
            Firing(
                m["name"],
                "dbt_test",
                sev,
                f"{m['name']}|{name}",
                f"model:{f['model']}",
                parts[-1],
                f"dbt test {name} on {f['model']}: {f['status']} in the latest build of "
                f"partition(s) {', '.join(parts)}",
                {
                    "test": name,
                    "model": f["model"],
                    "status": f["status"],
                    "partitions": parts,
                    "failures": f["failures"],
                },
            )
        )
    return out


def _task_failures(m: dict[str, Any], world: World, lookback: int) -> list[Firing]:
    runs = world.task_runs()
    ds_list = sorted({r["ds"] for r in runs})[-lookback:]
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for r in runs:
        if r["ds"] in ds_list:
            latest[(r["task_id"], r["ds"])] = r
    out: list[Firing] = []
    for (task, ds), r in sorted(latest.items()):
        if r["state"] != "failed":
            continue
        out.append(
            Firing(
                m["name"],
                "task_failure",
                m.get("severity", "high"),
                f"{m['name']}|{task}|{ds}",
                f"task:{task}",
                ds,
                f"harbor_daily.{task} failed for ds={ds} after attempt {r['attempt']}: {r['error']}",
                {"task": task, "ds": ds, "run_id": r["run_id"], "attempt": r["attempt"], "error": r["error"]},
            )
        )
    return out


def evaluate(world: World) -> list[Firing]:
    cfg = load_config(world)
    lookback = int(cfg.get("defaults", {}).get("lookback_partitions", 3))
    now = world.now()
    out: list[Firing] = []
    conn = world.connect(read_only=True)
    try:
        for m in cfg.get("monitors", []):
            if m.get("enabled", True) is False:
                continue
            kind = m.get("type")
            try:
                if kind == "freshness":
                    out += _freshness(m, world, conn, now)
                elif kind == "volume":
                    out += _volume(m, world, conn, now, int(m.get("lookback_partitions", lookback)))
                elif kind == "schema":
                    out += _schema(m, world)
                elif kind == "reconciliation":
                    out += _reconciliation(m, world, conn, lookback)
                elif kind == "dbt_tests":
                    out += _dbt_tests(m, conn, lookback)
                elif kind == "task_failure":
                    out += _task_failures(m, world, lookback)
            except Exception as exc:  # a broken monitor alerts about itself
                out.append(
                    Firing(
                        m.get("name", "?"),
                        "monitor_error",
                        "medium",
                        f"{m.get('name')}|error",
                        f"monitor:{m.get('name')}",
                        None,
                        f"monitor {m.get('name')} could not be evaluated: {type(exc).__name__}: {exc}",
                        {},
                    )
                )
    finally:
        conn.close()
    return out


def run(world: World) -> list[dict[str, Any]]:
    """Evaluate monitors and update the alert file. Returns the alert list."""
    firing = {f.key: f for f in evaluate(world)}
    now = fmt_ts(world.now())
    alerts = world.alerts()
    by_key = {a["_key"]: a for a in alerts}
    for key, f in firing.items():
        a = by_key.get(key)
        if a is None:
            a = {
                "_key": key,
                "id": alert_id(key),
                "monitor": f.monitor,
                "type": f.kind,
                "severity": f.severity,
                "status": "open",
                "target": f.target,
                "partition": f.partition,
                "opened_at": now,
            }
            alerts.append(a)
            by_key[key] = a
        elif a["status"] == "resolved":
            a["status"] = "open"
            a["reopened_at"] = now
        a["last_seen_at"] = now
        a["message"] = f.message
        a["details"] = f.details
    for a in alerts:
        if a["_key"] not in firing and a["status"] == "open":
            a["status"] = "resolved"
            a["resolved_at"] = now
    world.save_alerts(alerts)
    return alerts
