"""The oracle: replays each task's scripted correct solution through the tool API.

It reads the hidden ground truth to know what to do, then acts only through tools, like any
other agent. If it does not score 100% resolved, a task's ground truth is broken.
"""

from __future__ import annotations

from typing import Any

from triagebench.agents.base import AgentResult
from triagebench.episode import Episode


class OracleAgent:
    name = "oracle"

    def run(self, episode: Episode) -> AgentResult:
        gt = episode.ground_truth()
        task = gt["task"]
        api = episode.api
        alerts = api.call("list_alerts")["result"]
        values: dict[str, Any] = {
            **gt.get("placeholders", {}),
            "summary": f"{task['summary']} (alerts {', '.join(a['id'] for a in alerts)})",
            "resolution": "expected" if task["decision"] == "no_action" else "resolved",
        }
        evidence = [a["id"] for a in alerts]

        def fill(v: Any) -> Any:
            if isinstance(v, str):
                for k, val in values.items():
                    v = v.replace("{" + k + "}", str(val))
                return v
            if isinstance(v, list):
                if v == ["{evidence}"]:
                    return evidence
                return [fill(x) for x in v]
            if isinstance(v, dict):
                return {k: fill(x) for k, x in v.items()}
            return v

        for tool, args in task["oracle"]:
            if tool == "_close_all_open_alerts":
                for a in api.call("list_alerts")["result"]:
                    api.call(
                        "close_alert",
                        {"alert_id": a["id"], "resolution": fill(args["resolution"]), "summary": values["summary"]},
                    )
                continue
            res = api.call(tool, fill(args))
            if not res["ok"]:
                raise RuntimeError(f"oracle step {tool} failed on {task['id']}: {res.get('error')}")
        return AgentResult()
