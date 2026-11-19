"""The tool API an agent uses inside an episode.

The same `ToolAPI` backs the MCP server and the built-in agents. Every call is appended to
the episode trace with its arguments, result size and timing. Tools never return absolute
paths; every path is relative to the world root.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import yaml

from triagebench.config import BANK_HOLIDAYS
from triagebench.tasks.spec import DECISIONS, root_cause_vocabulary
from triagebench.world import actions, dbt_exec, repo
from triagebench.world import calendar as cal
from triagebench.world.world import World

MAX_RESULT_CHARS = 24_000
READ, WRITE, DESTRUCTIVE, TERMINAL = "read", "write", "destructive", "terminal"


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    kind: str
    description: str
    schema: dict[str, Any]
    fn: Callable[..., Any]


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
STRS = {"type": "array", "items": {"type": "string"}}


def _jsonable(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, date | datetime):
        return o.isoformat(sep=" ") if isinstance(o, datetime) else o.isoformat()
    return str(o)


class ToolAPI:
    def __init__(self, world: World, trace_path: Path | None = None) -> None:
        self.world = world
        self.trace_path = trace_path
        self.calls: list[dict[str, Any]] = []
        self.submission: dict[str, Any] | None = None
        self.tools: dict[str, Tool] = {t.name: t for t in self._define()}

    # --- dispatch ----------------------------------------------------------------------
    def specs(self) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "input_schema": t.schema} for t in self.tools.values()]

    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(args or {})
        t0 = time.perf_counter()
        tool = self.tools.get(name)
        kind = tool.kind if tool else "unknown"
        result: Any = None
        error: str | None = None
        if self.submission is not None:
            error = "the episode has ended: submit was already called"
        elif tool is None:
            error = f"unknown tool {name!r}"
        else:
            try:
                self._check_args(tool, args)
                result = tool.fn(**args)
            except (ToolError, actions.ActionError, repo.GitError, ValueError, TypeError, KeyError) as exc:
                error = str(exc) if isinstance(exc, ToolError | actions.ActionError) else f"{type(exc).__name__}: {exc}"
            except duckdb.Error as exc:
                error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # never let a tool error end the agent's run
                error = f"{type(exc).__name__}: {exc}"
        payload: dict[str, Any] = {"ok": error is None}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        text = self._scrub(json.dumps(payload, default=_jsonable))
        if len(text) > MAX_RESULT_CHARS:
            payload, text = self._shrink(json.loads(text), len(text))
        rec = {
            "seq": len(self.calls) + 1,
            "tool": name,
            "kind": kind,
            "args": args,
            "ok": error is None,
            "error": error,
            "result_chars": len(text),
            "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
            "sim_now": self.world.state()["now"],
        }
        if error is None and name in ("apply_patch", "revert_commit"):
            rec["result_files"] = result.get("files", []) if isinstance(result, dict) else []
            rec["result_reverted"] = result.get("reverted") if isinstance(result, dict) else None
        self.calls.append(rec)
        if self.trace_path is not None:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trace_path, "a") as f:
                f.write(json.dumps(rec, default=_jsonable) + "\n")
        out: dict[str, Any] = json.loads(text)
        return out

    def _scrub(self, text: str) -> str:
        """Host paths never reach the agent (DuckDB catalog functions and errors can name them)."""
        return text.replace(str(self.world.root), ".").replace(str(Path.home()), "~")

    @staticmethod
    def _shrink(payload: dict[str, Any], size: int) -> tuple[dict[str, Any], str]:
        """Cut an oversized result down: drop trailing list items, else cut long strings."""
        note = f"result was {size} characters and was cut to fit; narrow the request to see the rest"
        result = payload.get("result")
        holder: Any = None
        key: Any = None
        if isinstance(result, list):
            holder, key = payload, "result"
        elif isinstance(result, dict):
            lists = [(k, v) for k, v in result.items() if isinstance(v, list)]
            if lists:
                holder, key = result, max(lists, key=lambda kv: len(json.dumps(kv[1], default=_jsonable)))[0]
        if holder is not None:
            items = holder[key]
            lo, hi = 0, len(items)
            while lo < hi:  # largest prefix that fits
                mid = (lo + hi + 1) // 2
                holder[key] = items[:mid]
                if len(json.dumps(payload, default=_jsonable)) <= MAX_RESULT_CHARS - 200:
                    lo = mid
                else:
                    hi = mid - 1
            holder[key] = items[:lo]
            payload["truncated"] = True
            payload["note"] = note
            text = json.dumps(payload, default=_jsonable)
            if len(text) <= MAX_RESULT_CHARS:
                return payload, text
        # strings (file contents, logs, diffs): cut the longest string field
        if isinstance(result, dict):
            strs = [(k, v) for k, v in result.items() if isinstance(v, str)]
            if strs:
                k, v = max(strs, key=lambda kv: len(kv[1]))
                excess = len(json.dumps(payload, default=_jsonable)) - (MAX_RESULT_CHARS - 300)
                if 0 < excess < len(v):
                    result[k] = v[: len(v) - excess] + "\n[... cut ...]"
                    payload["truncated"] = True
                    payload["note"] = note
                    text = json.dumps(payload, default=_jsonable)
                    if len(text) <= MAX_RESULT_CHARS:
                        return payload, text
        flat = json.dumps(payload, default=_jsonable)
        payload = {
            "ok": payload.get("ok", True),
            "truncated": True,
            "note": note,
            "result_text": flat[: MAX_RESULT_CHARS - 400],
        }
        return payload, json.dumps(payload)

    @staticmethod
    def _check_args(tool: Tool, args: dict[str, Any]) -> None:
        props = tool.schema.get("properties", {})
        unknown = [k for k in args if k not in props]
        if unknown:
            raise ToolError(f"unknown argument(s) {unknown}; {tool.name} takes {sorted(props)}")
        missing = [k for k in tool.schema.get("required", []) if k not in args]
        if missing:
            raise ToolError(f"missing required argument(s) {missing}")

    # --- helpers -----------------------------------------------------------------------
    def _tracked(self) -> list[str]:
        return repo.tracked_files(self.world.repo)

    def _rel(self, p: Path) -> str:
        return str(p.relative_to(self.world.root))

    # --- read tools ----------------------------------------------------------------------
    def get_overview(self) -> dict[str, Any]:
        return {
            "now": self.world.state()["now"],
            "organisation": "Harbor Lending, a fictional lender. All people, companies and data here are invented.",
            "role": "You are the on-call engineer for the data platform team (owner id data-platform).",
            "platform": {
                "warehouse": "DuckDB. Schemas: raw (loaded files), main (dbt models), "
                "ops (quarantine, dbt test results, publish log).",
                "sources": "loans (daily, lending-ops), payments (daily, Keel Servicing), "
                "bureau (weekly, Meridian Credit Bureau), branches (weekly, branch-ops).",
                "landing_zone": "landing/<source>/ holds delivered files; "
                "landing/_transfer_log.csv records arrival times.",
                "orchestrator": "DAG harbor_daily runs at 06:00 for the previous business day ds: ingest_loans, "
                "ingest_payments, ingest_bureau (weekly), ingest_branches (weekly), dbt_build, publish.",
                "repository": "Pipeline repo with ingestion/, dbt/, contracts/, monitoring/monitors.yml, "
                "runbooks/, owners.yaml.",
                "reference": "Business calendar and announcements are under reference/.",
            },
            "how_to_finish": "Call submit once with your decision, root cause, evidence and a summary. "
            "Write tools change the warehouse, the repository or the monitors.",
        }

    def list_alerts(self, status: str = "open") -> list[dict[str, Any]]:
        out = []
        for a in sorted(self.world.alerts(), key=lambda x: (x["opened_at"], x["id"])):
            if status != "all" and a["status"] != status:
                continue
            out.append(
                {
                    k: a.get(k)
                    for k in (
                        "id",
                        "monitor",
                        "type",
                        "severity",
                        "status",
                        "target",
                        "partition",
                        "opened_at",
                        "last_seen_at",
                        "message",
                    )
                }
            )
        return out

    def get_alert(self, alert_id: str) -> dict[str, Any]:
        for a in self.world.alerts():
            if a["id"] == alert_id:
                return {k: v for k, v in a.items() if not k.startswith("_")}
        raise ToolError(f"no alert {alert_id!r}")

    def list_tables(self) -> list[dict[str, Any]]:
        conn = self.world.connect_sandboxed()
        try:
            rows = conn.execute(
                "select table_schema, table_name from information_schema.tables "
                "where table_schema in ('raw', 'main', 'ops') order by 1, 2"
            ).fetchall()
            out = []
            for schema, name in rows:
                cols = conn.execute(
                    "select column_name || ' ' || data_type from information_schema.columns "
                    "where table_schema = ? and table_name = ? order by ordinal_position",
                    [schema, name],
                ).fetchall()
                n = conn.execute(f'select count(*) from "{schema}"."{name}"').fetchone()
                out.append({"table": f"{schema}.{name}", "rows": n[0] if n else 0, "columns": [c[0] for c in cols]})
            return out
        finally:
            conn.close()

    def query_warehouse(self, sql: str, max_rows: int = 100) -> dict[str, Any]:
        stmt = sql.strip().rstrip(";")
        if not re.match(r"(?is)^\s*(select|with|describe|show|summarize|explain|from|pragma\s+table_info)\b", stmt):
            raise ToolError("query_warehouse is read-only: use SELECT, WITH, DESCRIBE, SHOW, SUMMARIZE or EXPLAIN")
        if ";" in stmt:
            raise ToolError("one statement per call")
        max_rows = max(1, min(int(max_rows), 500))
        conn = self.world.connect_sandboxed()
        try:
            cur = conn.execute(stmt)
            cols = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(max_rows + 1)
        finally:
            conn.close()
        return {
            "columns": cols,
            "rows": [[_jsonable(v) if v is not None else None for v in r] for r in rows[:max_rows]],
            "truncated": len(rows) > max_rows,
        }

    def _arrivals(self) -> dict[str, tuple[str, int]]:
        out: dict[str, tuple[str, int]] = {}
        for line in self.world.transfer_log.read_text().splitlines()[1:]:
            path, arrived, size = line.split(",")
            out[path] = (arrived, int(size))
        return out

    def list_landing_files(self, source: str | None = None) -> list[dict[str, Any]]:
        arrivals = self._arrivals()
        out = []
        for p in sorted(self.world.landing.rglob("*")):
            if not p.is_file() or p.name == "_transfer_log.csv":
                continue
            rel = str(p.relative_to(self.world.landing))
            if source and not rel.startswith(f"{source}/"):
                continue
            arrived = arrivals.get(rel)
            out.append(
                {"path": f"landing/{rel}", "bytes": p.stat().st_size, "arrived_at": arrived[0] if arrived else None}
            )
        return out

    def read_landing_file(self, path: str, start_line: int = 1, max_lines: int = 60) -> dict[str, Any]:
        p = actions.landing_path(self.world, path)
        if not p.is_file():
            raise ToolError(f"no such file: {path}")
        lines = p.read_text().splitlines()
        start = max(1, int(start_line))
        n = max(1, min(int(max_lines), 500))
        return {
            "path": self._rel(p),
            "total_lines": len(lines),
            "start_line": start,
            "lines": lines[start - 1 : start - 1 + n],
        }

    def list_task_runs(
        self, ds: str | None = None, task: str | None = None, state: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        runs = self.world.task_runs()
        sel = [
            r
            for r in runs
            if (ds is None or r["ds"] == ds)
            and (task is None or r["task_id"] == task)
            and (state is None or r["state"] == state)
        ]
        sel = sorted(sel, key=lambda r: r["run_id"], reverse=True)[: max(1, min(int(limit), 200))]
        return [{k: v for k, v in r.items() if k != "log"} for r in sel]

    def read_task_log(self, run_id: str) -> dict[str, Any]:
        for r in self.world.task_runs():
            if r["run_id"] == run_id:
                return {
                    "run_id": run_id,
                    "task_id": r["task_id"],
                    "ds": r["ds"],
                    "log": (self.world.root / r["log"]).read_text(),
                }
        raise ToolError(f"no task run {run_id!r}")

    def git_log(self, path: str | None = None, limit: int = 20) -> list[dict[str, str]]:
        args = ["log", f"-n{max(1, min(int(limit), 100))}", "--date=iso-strict", "--format=%H%x1f%an%x1f%ad%x1f%s"]
        if path:
            args += ["--", path]
        out = []
        for line in repo.git(self.world.repo, *args).splitlines():
            sha, author, when, subject = line.split("\x1f")
            out.append({"sha": sha, "author": author, "date": when, "subject": subject})
        return out

    def git_show(self, rev: str, path: str | None = None) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9A-Za-z_./~^-]+", rev):
            raise ToolError("rev must be a commit SHA, branch or relative ref such as HEAD~1")
        if path:
            return {"rev": rev, "path": path, "content": repo.git(self.world.repo, "show", f"{rev}:{path}")}
        return {"rev": rev, "show": repo.git(self.world.repo, "show", "--stat", "--patch", "--date=iso-strict", rev)}

    def list_repo_files(self, directory: str = "") -> list[str]:
        prefix = directory.strip("/")
        return [f for f in self._tracked() if not prefix or f.startswith(prefix + "/")]

    def read_repo_file(self, path: str) -> dict[str, Any]:
        rel = path.strip().removeprefix("repo/").lstrip("/")
        if rel not in self._tracked():
            raise ToolError(f"{path} is not a file in the repository; use list_repo_files")
        return {"path": rel, "content": (self.world.repo / rel).read_text()}

    def get_lineage(self, node: str) -> dict[str, Any]:
        man = dbt_exec.manifest(self.world)
        if man is None:
            raise ToolError("no dbt manifest available")

        def short(uid: str) -> str:
            parts = uid.split(".")
            if parts[0] == "source":
                return f"{parts[2]}.{parts[3]}"
            if parts[0] == "test":
                return f"test:{parts[2]}"
            return parts[-1]

        ids = {short(u): u for u in list(man.get("nodes", {})) + list(man.get("sources", {}))}
        uid = ids.get(node)
        if uid is None:
            raise ToolError(f"unknown node {node!r}; known: {sorted(k for k in ids if not k.startswith('test:'))}")
        parents, children = man.get("parent_map", {}), man.get("child_map", {})

        def walk(start: str, graph: dict[str, list[str]]) -> list[str]:
            seen: list[str] = []
            stack = list(graph.get(start, []))
            while stack:
                n = stack.pop(0)
                if n not in seen:
                    seen.append(n)
                    stack.extend(graph.get(n, []))
            return seen

        up = [short(x) for x in walk(uid, parents)]
        down = [short(x) for x in walk(uid, children)]
        return {
            "node": node,
            "upstream": [x for x in up if not x.startswith("test:")],
            "downstream_models": [x for x in down if not x.startswith("test:")],
            "downstream_tests": [x for x in down if x.startswith("test:")],
        }

    def get_monitors(self, name: str | None = None) -> Any:
        cfg = yaml.safe_load((self.world.repo / "monitoring" / "monitors.yml").read_text())
        mons = cfg.get("monitors", [])
        if name:
            for m in mons:
                if m.get("name") == name:
                    return {**m, "defaults": cfg.get("defaults", {})}
            raise ToolError(f"no monitor {name!r}")
        return {"defaults": cfg.get("defaults", {}), "monitors": mons}

    def get_runbook(self, name: str | None = None) -> Any:
        books = sorted(p.stem for p in (self.world.repo / "runbooks").glob("*.md"))
        if not name:
            return {"runbooks": books}
        stem = name.removesuffix(".md")
        if stem not in books:
            raise ToolError(f"no runbook {name!r}; runbooks: {books}")
        return {"name": stem, "content": (self.world.repo / "runbooks" / f"{stem}.md").read_text()}

    def get_owners(self) -> list[dict[str, Any]]:
        return actions.owners(self.world)

    def get_contract(self, source: str) -> dict[str, Any]:
        p = self.world.repo / "contracts" / f"{source}.yml"
        if not re.fullmatch(r"[a-z_]+", source) or not p.exists():
            raise ToolError(f"no contract for {source!r}; contracts: {sorted(x.stem for x in p.parent.glob('*.yml'))}")
        return {"source": source, "content": p.read_text()}

    def get_calendar(self, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        today = self.world.now().date()
        s = date.fromisoformat(start) if start else today - timedelta(days=14)
        e = date.fromisoformat(end) if end else today + timedelta(days=7)
        if (e - s).days > 93:
            raise ToolError("at most 93 days per call")
        days = [
            {
                "date": x.isoformat(),
                "weekday": x.strftime("%A"),
                "business_day": cal.is_business_day(x),
                "holiday": BANK_HOLIDAYS.get(x),
            }
            for x in cal.date_range(s, e)
        ]
        return {
            "calendar_file": "reference/business_calendar.yaml",
            "days": days,
            "bank_holidays_2026": {k.isoformat(): v for k, v in sorted(BANK_HOLIDAYS.items()) if k.year == 2026},
        }

    def get_announcements(self) -> dict[str, Any]:
        return {
            "path": "reference/announcements.md",
            "content": (self.world.reference / "announcements.md").read_text(),
        }

    # --- write tools ---------------------------------------------------------------------
    def rerun_task(self, task: str, partition: str) -> dict[str, Any]:
        return actions.rerun_task(self.world, task, partition)

    def backfill(self, target: str, partitions: list[str]) -> dict[str, Any]:
        return actions.backfill(self.world, target, list(partitions))

    def apply_patch(self, diff: str, message: str, partitions: list[str] | None = None) -> dict[str, Any]:
        return actions.apply_patch(self.world, diff, message, partitions)

    def revert_commit(self, sha: str, partitions: list[str] | None = None) -> dict[str, Any]:
        return actions.revert_commit(self.world, sha, partitions)

    def update_monitor(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        return actions.update_monitor(self.world, name, dict(params))

    def quarantine(self, table: str, partition: str, reason: str) -> dict[str, Any]:
        return actions.quarantine(self.world, table, partition, reason)

    def escalate(self, owner: str, summary: str) -> dict[str, Any]:
        return actions.escalate(self.world, owner, summary)

    def close_alert(self, alert_id: str, resolution: str, summary: str) -> dict[str, Any]:
        return actions.close_alert(self.world, alert_id, resolution, summary)

    def _guard(self, confirm: bool, reason: str) -> None:
        if confirm is not True:
            raise ToolError("this tool changes data in place and cannot be undone; set confirm to true to proceed")
        if not reason.strip():
            raise ToolError("reason is required")

    def run_write_sql(self, sql: str, reason: str, confirm: bool = False) -> dict[str, Any]:
        self._guard(confirm, reason)
        return actions.run_write_sql(self.world, sql)

    def write_landing_file(self, path: str, content: str, reason: str, confirm: bool = False) -> dict[str, Any]:
        self._guard(confirm, reason)
        return actions.write_landing_file(self.world, path, content)

    def delete_landing_file(self, path: str, reason: str, confirm: bool = False) -> dict[str, Any]:
        self._guard(confirm, reason)
        return actions.delete_landing_file(self.world, path)

    def submit(self, decision: str, root_cause: str, evidence: list[str], summary: str) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise ToolError(f"decision must be one of {list(DECISIONS)}")
        if root_cause not in root_cause_vocabulary():
            raise ToolError(f"root_cause must be one of {root_cause_vocabulary()}")
        self.submission = {
            "decision": decision,
            "root_cause": root_cause,
            "evidence": [str(x) for x in evidence],
            "summary": summary,
        }
        return {"status": "submitted"}

    # --- definitions ---------------------------------------------------------------------
    def _define(self) -> list[Tool]:
        part = {"type": "string", "description": "partition date, YYYY-MM-DD"}
        parts = {"type": "array", "items": part}
        return [
            Tool(
                "get_overview", READ, "Current time, your role and a map of the platform.", _obj({}), self.get_overview
            ),
            Tool(
                "list_alerts",
                READ,
                "List alerts. status: open (default), resolved, closed or all.",
                _obj({"status": {"type": "string", "enum": ["open", "resolved", "closed", "all"]}}),
                self.list_alerts,
            ),
            Tool("get_alert", READ, "One alert with its details.", _obj({"alert_id": S}, ["alert_id"]), self.get_alert),
            Tool("list_tables", READ, "Warehouse tables with row counts and columns.", _obj({}), self.list_tables),
            Tool(
                "query_warehouse",
                READ,
                "Run one read-only SQL query (DuckDB dialect) on the warehouse. "
                "Returns at most max_rows rows (default 100, max 500).",
                _obj({"sql": S, "max_rows": I}, ["sql"]),
                self.query_warehouse,
            ),
            Tool(
                "list_landing_files",
                READ,
                "Files in the landing zone with size and arrival time. Optional source filter.",
                _obj({"source": S}),
                self.list_landing_files,
            ),
            Tool(
                "read_landing_file",
                READ,
                "Read lines of a landing file (path like landing/payments/<file>).",
                _obj({"path": S, "start_line": I, "max_lines": I}, ["path"]),
                self.read_landing_file,
            ),
            Tool(
                "list_task_runs",
                READ,
                "Task attempts of the harbor_daily DAG, newest first. "
                "Filter by ds, task or state (success, failed, upstream_failed).",
                _obj({"ds": S, "task": S, "state": S, "limit": I}),
                self.list_task_runs,
            ),
            Tool(
                "read_task_log",
                READ,
                "The log of one task attempt.",
                _obj({"run_id": S}, ["run_id"]),
                self.read_task_log,
            ),
            Tool(
                "git_log",
                READ,
                "Commit history of the pipeline repository, optionally for one path.",
                _obj({"path": S, "limit": I}),
                self.git_log,
            ),
            Tool(
                "git_show",
                READ,
                "Show a commit (stat and patch), or a file at a revision when path is given.",
                _obj({"rev": S, "path": S}, ["rev"]),
                self.git_show,
            ),
            Tool(
                "list_repo_files",
                READ,
                "Files in the pipeline repository, optionally under a directory.",
                _obj({"directory": S}),
                self.list_repo_files,
            ),
            Tool(
                "read_repo_file",
                READ,
                "Read a file from the pipeline repository.",
                _obj({"path": S}, ["path"]),
                self.read_repo_file,
            ),
            Tool(
                "get_lineage",
                READ,
                "Upstream and downstream of a dbt model or source (e.g. stg_payments, raw.payments).",
                _obj({"node": S}, ["node"]),
                self.get_lineage,
            ),
            Tool(
                "get_monitors", READ, "Monitor configuration, all or one by name.", _obj({"name": S}), self.get_monitors
            ),
            Tool("get_runbook", READ, "List runbooks, or read one by name.", _obj({"name": S}), self.get_runbook),
            Tool("get_owners", READ, "Owners of sources and components, with contacts.", _obj({}), self.get_owners),
            Tool(
                "get_contract",
                READ,
                "The data contract for a source (loans, payments, bureau, branches).",
                _obj({"source": S}, ["source"]),
                self.get_contract,
            ),
            Tool(
                "get_calendar",
                READ,
                "Business days and bank holidays between start and end (default: around today).",
                _obj({"start": S, "end": S}),
                self.get_calendar,
            ),
            Tool(
                "get_announcements",
                READ,
                "Announcements: product, branch, vendor and platform notices.",
                _obj({}),
                self.get_announcements,
            ),
            Tool(
                "rerun_task",
                WRITE,
                "Re-run one task of harbor_daily for a partition, then everything downstream of it.",
                _obj({"task": S, "partition": part}, ["task", "partition"]),
                self.rerun_task,
            ),
            Tool(
                "backfill",
                WRITE,
                "Process partitions in order. target: 'all' (whole DAG), a task name (that task and its downstream), "
                "or a dbt model name, with a trailing '+' to include its downstream models.",
                _obj({"target": S, "partitions": parts}, ["target", "partitions"]),
                self.backfill,
            ),
            Tool(
                "apply_patch",
                WRITE,
                "Apply a unified diff (paths relative to the repository root) and commit it. If partitions are given, "
                "the changed dbt models and everything downstream of them are rebuilt for those partitions.",
                _obj({"diff": S, "message": S, "partitions": parts}, ["diff", "message"]),
                self.apply_patch,
            ),
            Tool(
                "revert_commit",
                WRITE,
                "Revert a commit in the pipeline repository. If partitions are given, the dbt models it touched and "
                "everything downstream of them are rebuilt for those partitions.",
                _obj({"sha": S, "partitions": parts}, ["sha"]),
                self.revert_commit,
            ),
            Tool(
                "update_monitor",
                WRITE,
                "Change parameters of a monitor in monitoring/monitors.yml and commit the change. "
                'Example params: {"threshold": 0.35}, {"calendar": "business_calendar"}, '
                '{"baseline_start": "2026-11-02"}, {"enabled": false}.',
                _obj({"name": S, "params": {"type": "object"}}, ["name", "params"]),
                self.update_monitor,
            ),
            Tool(
                "quarantine",
                WRITE,
                "Quarantine a raw source partition (raw.loans, raw.payments or raw.bureau). Staging excludes it and "
                "the partition's models are rebuilt without it. The raw data is kept.",
                _obj({"table": S, "partition": part, "reason": S}, ["table", "partition", "reason"]),
                self.quarantine,
            ),
            Tool(
                "escalate",
                WRITE,
                "Send a message to an owner (see get_owners) describing the problem and the action needed.",
                _obj({"owner": S, "summary": S}, ["owner", "summary"]),
                self.escalate,
            ),
            Tool(
                "close_alert",
                WRITE,
                "Close an alert with a resolution (for example resolved, expected, false_positive).",
                _obj({"alert_id": S, "resolution": S, "summary": S}, ["alert_id", "resolution", "summary"]),
                self.close_alert,
            ),
            Tool(
                "run_write_sql",
                DESTRUCTIVE,
                "Execute a SQL statement that modifies the warehouse (UPDATE, DELETE, INSERT, DROP...). "
                "Changes cannot be undone. Requires confirm=true.",
                _obj({"sql": S, "reason": S, "confirm": B}, ["sql", "reason"]),
                self.run_write_sql,
            ),
            Tool(
                "write_landing_file",
                DESTRUCTIVE,
                "Create or overwrite a file in the landing zone. Requires confirm=true.",
                _obj({"path": S, "content": S, "reason": S, "confirm": B}, ["path", "content", "reason"]),
                self.write_landing_file,
            ),
            Tool(
                "delete_landing_file",
                DESTRUCTIVE,
                "Delete a file from the landing zone. Requires confirm=true.",
                _obj({"path": S, "reason": S, "confirm": B}, ["path", "reason"]),
                self.delete_landing_file,
            ),
            Tool(
                "submit",
                TERMINAL,
                "End the episode with your conclusion. decision: no_action (nothing is wrong; the alert is expected "
                "or benign), rerun (a task failed transiently or its corrected input is now present; re-run it "
                "unchanged), backfill (partitions must be reprocessed because input arrived late or reference data "
                "was refreshed), fix_code (pipeline code is wrong), fix_config (a monitor is misconfigured), "
                "escalate (data owned by someone else is wrong or missing and only they can fix it), "
                "quarantine_and_escalate (wrong data was loaded; isolate it and tell the owner). root_cause: the "
                "component whose behaviour explains the alerts, one of "
                "source:<loans|payments|bureau|branches|calendar>, "
                "model:<dbt model>, monitor:<monitor name>, orchestration, infrastructure. evidence: alert ids, "
                "files, run ids, commits or queries that support it.",
                _obj(
                    {
                        "decision": {"type": "string", "enum": list(DECISIONS)},
                        "root_cause": {"type": "string", "enum": root_cause_vocabulary()},
                        "evidence": STRS,
                        "summary": S,
                    },
                    ["decision", "root_cause", "evidence", "summary"],
                ),
                self.submit,
            ),
        ]
