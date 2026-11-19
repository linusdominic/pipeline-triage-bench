"""Naive scripted agents. They use only the agent tools, never the ground truth.

* always_rerun: re-run every failed task (or the latest dbt_build) and call it transient.
* always_escalate: send the alerts to the owner of the source they mention.
* always_close: close every alert as a false positive.
* fix_everything: take the most plausible-looking write action for each alert.
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta
from typing import Any, ClassVar

from triagebench.agents.base import AgentResult
from triagebench.episode import Episode
from triagebench.tools import ToolAPI

OWNER_OF = {"payments": "keel-servicing", "bureau": "meridian-bureau", "loans": "lending-ops", "branches": "branch-ops"}


def _alerts(api: ToolAPI) -> list[dict[str, Any]]:
    res: list[dict[str, Any]] = api.call("list_alerts")["result"]
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(res, key=lambda a: (order.get(a["severity"], 3), a["opened_at"], a["id"]))


def _source_of(alert: dict[str, Any]) -> str | None:
    text = f"{alert.get('target', '')} {alert.get('message', '')}"
    for src in ("payments", "bureau", "loans", "branches"):
        if re.search(rf"\b(raw\.{src}|source:{src}|ingest_{src}|landing/{src})\b", text):
            return src
    return None


def _root_from(alerts: list[dict[str, Any]], default: str) -> str:
    for a in alerts:
        src = _source_of(a)
        if src:
            return f"source:{src}"
    return default


def _latest_failed(api: ToolAPI) -> list[tuple[str, str]]:
    runs = api.call("list_task_runs", {"limit": 60})["result"]
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for r in sorted(runs, key=lambda r: r["run_id"]):
        latest[(r["task_id"], r["ds"])] = r
    return sorted(k for k, r in latest.items() if r["state"] == "failed")


def _submit(api: ToolAPI, decision: str, root: str, alerts: list[dict[str, Any]], summary: str) -> None:
    api.call(
        "submit", {"decision": decision, "root_cause": root, "evidence": [a["id"] for a in alerts], "summary": summary}
    )


class AlwaysRerun:
    name = "always_rerun"

    def run(self, episode: Episode) -> AgentResult:
        api = episode.api
        alerts = _alerts(api)
        failed = _latest_failed(api)
        if failed:
            for task, ds in failed:
                api.call("rerun_task", {"task": task, "partition": ds})
        else:
            runs = api.call("list_task_runs", {"task": "dbt_build", "limit": 1})["result"]
            if runs:
                api.call("rerun_task", {"task": "dbt_build", "partition": runs[0]["ds"]})
        _submit(api, "rerun", "infrastructure", alerts, "Re-ran the failed tasks; treating this as transient.")
        return AgentResult()


class AlwaysEscalate:
    name = "always_escalate"

    def run(self, episode: Episode) -> AgentResult:
        api = episode.api
        alerts = _alerts(api)
        src = next((s for s in (_source_of(a) for a in alerts) if s), None)
        owner = OWNER_OF.get(src or "", "data-platform")
        body = "; ".join(f"{a['id']}: {a['message']}" for a in alerts)
        api.call("escalate", {"owner": owner, "summary": f"Please investigate: {body}"})
        _submit(api, "escalate", f"source:{src}" if src else "infrastructure", alerts, f"Escalated to {owner}.")
        return AgentResult()


class AlwaysClose:
    name = "always_close"

    def run(self, episode: Episode) -> AgentResult:
        api = episode.api
        alerts = _alerts(api)
        for a in alerts:
            api.call("close_alert", {"alert_id": a["id"], "resolution": "false_positive", "summary": "Not actionable."})
        root = _root_from(alerts, f"monitor:{alerts[0]['monitor']}" if alerts else "infrastructure")
        _submit(api, "no_action", root, alerts, "Closed all alerts as false positives.")
        return AgentResult()


class FixEverything:
    """Acts on every alert with the write action that looks most plausible for its type."""

    name = "fix_everything"
    FIRST_ACTION_DECISION: ClassVar[dict[str, str]] = {
        "apply_patch": "fix_code",
        "revert_commit": "fix_code",
        "run_write_sql": "fix_code",
        "update_monitor": "fix_config",
        "rerun_task": "rerun",
        "backfill": "backfill",
    }

    def run(self, episode: Episode) -> AgentResult:
        api = episode.api
        alerts = _alerts(api)
        done: set[str] = set()
        first: str | None = None

        def act(tool: str, args: dict[str, Any]) -> bool:
            nonlocal first
            key = f"{tool}:{sorted(args.items())}"
            if key in done:
                return False
            done.add(key)
            ok = bool(api.call(tool, args)["ok"])
            if ok and first is None:
                first = tool
            return ok

        for a in alerts:
            detail = api.call("get_alert", {"alert_id": a["id"]})["result"]
            d = detail.get("details", {})
            kind = a["type"]
            if kind == "schema":
                self._patch_mapping(api, act, d, a)
            elif kind == "reconciliation":
                ds = a["partition"]
                ratio = d["loaded_total"] / d["trailer_total"] if d.get("trailer_total") else 1
                if ratio > 10:
                    act(
                        "run_write_sql",
                        {
                            "sql": "update raw.payments set amount = amount / 100, "
                            "principal_amount = principal_amount / 100, "
                            f"interest_amount = interest_amount / 100 where _ds = '{ds}'",
                            "reason": "amounts loaded in cents",
                            "confirm": True,
                        },
                    )
                act("backfill", {"target": "dbt_build", "partitions": [ds]})
            elif kind == "volume":
                thr = float(d.get("threshold", 0.35))
                new = round(max(thr * 2, abs(float(d.get("relative_change", 0))) + 0.1), 2)
                act("update_monitor", {"name": a["monitor"], "params": {"threshold": new}})
            elif kind == "freshness":
                act("backfill", {"target": "all", "partitions": [d.get("expected_partition") or a["partition"]]})
            elif kind == "task_failure":
                act("rerun_task", {"task": d["task"], "partition": d["ds"]})
            elif kind == "dbt_test":
                self._dbt(api, act, d)
        root = _root_from(alerts, f"monitor:{alerts[0]['monitor']}" if alerts else "infrastructure")
        decision = self.FIRST_ACTION_DECISION.get(first or "", "no_action")
        if first in ("revert_commit", "apply_patch") and any(a["type"] == "dbt_test" for a in alerts):
            model = next((a["target"].removeprefix("model:") for a in alerts if a["type"] == "dbt_test"), None)
            root = f"model:{model}" if model else root
        if first == "update_monitor":
            root = f"monitor:{next(a['monitor'] for a in alerts if a['type'] == 'volume')}"
        _submit(api, decision, root, alerts, f"Applied fixes for {len(alerts)} alert(s).")
        return AgentResult()

    @staticmethod
    def _patch_mapping(api: ToolAPI, act: Any, d: dict[str, Any], alert: dict[str, Any]) -> None:
        missing, unexpected = d.get("missing_columns", []), d.get("unexpected_columns", [])
        if len(missing) != 1 or len(unexpected) != 1:
            return
        path = "ingestion/harbor_ingest.py"
        old = api.call("read_repo_file", {"path": path})["result"]["content"]
        anchor = "    header = next(csv.reader([lines[0]]))\n"
        if anchor not in old:
            return
        new = old.replace(
            anchor, anchor + f"    header = [{missing[0]!r} if c == {unexpected[0]!r} else c for c in header]\n"
        )
        diff = "".join(
            difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}")
        )
        act("apply_patch", {"diff": diff, "message": f"Accept {unexpected[0]} as {missing[0]}"})
        m = re.search(r"_(\d{4})(\d{2})(\d{2})(?:_r\d+)?\.csv", d.get("file", ""))
        if m:
            ds = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            src = alert["target"].removeprefix("source:")
            act("backfill", {"target": f"ingest_{src}", "partitions": [ds]})

    @staticmethod
    def _dbt(api: ToolAPI, act: Any, d: dict[str, Any]) -> None:
        parts = d.get("partitions", [])
        now = datetime.fromisoformat(api.call("get_overview")["result"]["now"])
        commits = api.call("git_log", {"path": "dbt/models", "limit": 5})["result"]
        recent = [
            c for c in commits if datetime.fromisoformat(c["date"]).replace(tzinfo=None) > now - timedelta(days=3)
        ]
        if recent and d.get("status") != "warn":
            act("revert_commit", {"sha": recent[0]["sha"]})
            act("backfill", {"target": "dbt_build", "partitions": parts})
        else:
            for p in parts:
                act("rerun_task", {"task": "dbt_build", "partition": p})
