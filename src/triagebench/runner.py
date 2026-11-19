"""Run agents over tasks, in parallel worker processes, and collect scores."""

from __future__ import annotations

import json
import multiprocessing as mp
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from triagebench.agents.base import Agent
from triagebench.config import DEFAULT_SEED
from triagebench.episode import Episode
from triagebench.scoring import aggregate

SCRIPTED_AGENTS = ("oracle", "always_rerun", "always_escalate", "always_close", "fix_everything")


def make_agent(name: str) -> Agent:
    from triagebench.agents import baselines, oracle

    agents: dict[str, Agent] = {
        "oracle": oracle.OracleAgent(),
        "always_rerun": baselines.AlwaysRerun(),
        "always_escalate": baselines.AlwaysEscalate(),
        "always_close": baselines.AlwaysClose(),
        "fix_everything": baselines.FixEverything(),
    }
    if name not in agents:
        raise KeyError(f"unknown agent {name!r}; scripted agents: {sorted(agents)} (the LLM agent runs via run-llm)")
    return agents[name]


def run_episode(agent: Agent, task_id: str, seed: int, trace_dir: Path | None) -> dict[str, Any]:
    t0 = time.time()
    trace = trace_dir / f"{task_id}.trace.jsonl" if trace_dir else None
    if trace and trace.exists():
        trace.unlink()
    with Episode(task_id, seed, trace) as ep:
        error = None
        try:
            res = agent.run(ep)
        except Exception as exc:  # an agent crash scores as whatever state it left
            error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            res = None
        score = ep.score(res.input_tokens if res else None, res.output_tokens if res else None)
    out = score.to_dict()
    out["agent"] = agent.name
    out["seconds"] = round(time.time() - t0, 2)
    out["agent_error"] = error
    if res is not None and res.cost_usd is not None:
        out["cost_usd"] = res.cost_usd
    return out


def _worker(args: tuple[str, str, int, str | None]) -> dict[str, Any]:
    name, task_id, seed, trace_dir = args
    return run_episode(make_agent(name), task_id, seed, Path(trace_dir) if trace_dir else None)


def run_agents(
    agent_names: list[str],
    task_ids: list[str],
    seed: int = DEFAULT_SEED,
    jobs: int = 4,
    out_dir: Path | None = None,
    log: Any = print,
) -> dict[str, list[dict[str, Any]]]:
    jobs_list = []
    for name in agent_names:
        trace_dir = out_dir / name if out_dir else None
        if trace_dir:
            trace_dir.mkdir(parents=True, exist_ok=True)
        jobs_list += [(name, t, seed, str(trace_dir) if trace_dir else None) for t in task_ids]
    results: dict[str, list[dict[str, Any]]] = {n: [] for n in agent_names}
    if jobs <= 1:
        it: Any = map(_worker, jobs_list)
        for r in it:
            results[r["agent"]].append(r)
            log(_line(r))
    else:
        with ProcessPoolExecutor(max_workers=jobs, mp_context=mp.get_context("spawn")) as ex:
            for r in ex.map(_worker, jobs_list):
                results[r["agent"]].append(r)
                log(_line(r))
    if out_dir:
        for name, rows in results.items():
            (out_dir / name / "scores.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            (out_dir / name / "summary.json").write_text(json.dumps(aggregate(rows), indent=2) + "\n")
    return results


def _line(r: dict[str, Any]) -> str:
    mark = "RESOLVED" if r["resolved"] else "-"
    extra = f" error={r['agent_error']}" if r.get("agent_error") else ""
    return (
        f"  {r['agent']:<16} {r['task']:<20} {mark:<8} decision={r['decision']} "
        f"(expected {r['expected_decision']}) outcome={'ok' if r['outcome_correct'] else 'wrong'} "
        f"unnecessary={r['unnecessary_actions']} forbidden={r['forbidden_actions']} "
        f"calls={r['tool_calls']} {r['seconds']}s{extra}"
    )
