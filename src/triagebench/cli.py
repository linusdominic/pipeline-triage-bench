"""Command-line interface: triagebench <command>."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from triagebench.config import DEFAULT_SEED

METRICS = [
    ("resolved_rate", "Resolved"),
    ("decision_accuracy", "Decision"),
    ("root_cause_accuracy", "Root cause"),
    ("outcome_accuracy", "End state"),
    ("false_alarm_restraint_rate", "False-alarm restraint"),
    ("unnecessary_action_rate", "Unnecessary-action rate"),
    ("destructive_action_rate", "Destructive-action rate"),
    ("mean_tool_calls", "Mean tool calls"),
]


def _pct(v: Any) -> str:
    return "n/a" if v is None else f"{v * 100:.0f}%"


def table(summaries: dict[str, dict[str, Any]]) -> str:
    head = "| Agent | " + " | ".join(label for _, label in METRICS) + " |"
    sep = "| --- |" + " --- |" * len(METRICS)
    rows = [head, sep]
    for agent, s in summaries.items():
        o = s["overall"]
        cells = [str(o[k]) if k == "mean_tool_calls" else _pct(o[k]) for k, _ in METRICS]
        rows.append(f"| {agent} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def family_table(summaries: dict[str, dict[str, Any]]) -> str:
    agents = list(summaries)
    families = list(next(iter(summaries.values()))["by_family"])
    rows = ["| Family | " + " | ".join(agents) + " |", "| --- |" + " --- |" * len(agents)]
    for f in families:
        rows.append(
            f"| {f} | " + " | ".join(_pct(summaries[a]["by_family"][f]["resolved_rate"]) for a in agents) + " |"
        )
    return "\n".join(rows)


def _task_ids(arg: list[str] | None) -> list[str]:
    from triagebench.tasks import catalog

    if not arg:
        return [t.id for t in catalog.all_tasks()]
    out = []
    for a in arg:
        for part in a.split(","):
            part = part.strip()
            if part in catalog.FAMILIES:
                out += [t.id for t in catalog.all_tasks() if t.family == part]
            elif part:
                catalog.get(part)
                out.append(part)
    return out


def cmd_list(args: argparse.Namespace) -> int:
    from triagebench.tasks import build, catalog

    rows = []
    for t in catalog.all_tasks():
        rows.append(
            {
                "id": t.id,
                "family": t.family,
                "family_title": catalog.FAMILIES[t.family],
                "incident_ds": t.incident_ds.isoformat(),
                "noise": bool(t.noise),
                "generated": build.is_built(args.seed, t.id),
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['id']:<22} {r['family_title']:<48} {r['incident_ds']}  {'generated' if r['generated'] else '-'}")
        print(f"{len(rows)} tasks in {len(catalog.FAMILIES)} families")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    from triagebench.tasks import build

    t0 = time.time()
    results = build.generate(args.seed, _task_ids(args.tasks) if args.tasks else None, jobs=args.jobs, force=args.force)
    bad = [r["task"] for r in results if not r["alerts_ok"]]
    print(f"done in {time.time() - t0:.1f}s")
    if bad:
        print(f"alert mismatch in: {bad}", file=sys.stderr)
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from triagebench import runner, scoring
    from triagebench.tasks import build

    ids = _task_ids(args.tasks)
    build.generate(args.seed, ids, jobs=args.jobs, log=lambda s: None)
    out = Path(args.out) if args.out else Path("runs") / f"{datetime.now():%Y%m%d-%H%M%S}"
    t0 = time.time()
    res = runner.run_agents(args.agent, ids, args.seed, args.jobs, out)
    summaries = {a: scoring.aggregate(rows) for a, rows in res.items()}
    print(table(summaries))
    print(f"\n{sum(len(r) for r in res.values())} episodes in {time.time() - t0:.1f}s; scores and traces in {out}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from triagebench import scoring

    run = Path(args.run_dir)
    summaries = {}
    for scores in sorted(run.glob("*/scores.jsonl")):
        rows = [json.loads(x) for x in scores.read_text().splitlines() if x.strip()]
        summaries[scores.parent.name] = scoring.aggregate(rows)
        (scores.parent / "summary.json").write_text(json.dumps(summaries[scores.parent.name], indent=2) + "\n")
    if not summaries:
        print(f"no */scores.jsonl under {run}", file=sys.stderr)
        return 1
    print(table(summaries))
    print()
    print(family_table(summaries))
    return 0


def cmd_baselines(args: argparse.Namespace) -> int:
    from triagebench import runner, scoring
    from triagebench.tasks import build

    ids = _task_ids(None)
    t0 = time.time()
    build.generate(args.seed, ids, jobs=args.jobs, log=lambda s: None)
    t_gen = time.time() - t0
    out = Path(args.runs) / "baselines"
    t1 = time.time()
    res = runner.run_agents(list(runner.SCRIPTED_AGENTS), ids, args.seed, args.jobs, out, log=lambda s: None)
    wall = time.time() - t1
    summaries = {a: scoring.aggregate(rows) for a, rows in res.items()}
    episode_seconds = [r["seconds"] for rows in res.values() for r in rows]
    meta = {
        "command": f"triagebench baselines --jobs {args.jobs} --seed {args.seed}",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "seed": args.seed,
        "tasks": len(ids),
        "agents": list(summaries),
        "episodes": len(episode_seconds),
        "jobs": args.jobs,
        "wall_seconds": round(wall, 1),
        "generation_check_seconds": round(t_gen, 1),
        "mean_episode_seconds": round(sum(episode_seconds) / len(episode_seconds), 2),
        "max_episode_seconds": round(max(episode_seconds), 2),
        "machine": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
    }
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)
    (results / "baselines.json").write_text(json.dumps({"meta": meta, "summaries": summaries}, indent=2) + "\n")
    per_task = {
        a: {
            r["task"]: {
                "resolved": r["resolved"],
                "decision": r["decision"],
                "unnecessary": r["unnecessary_actions"],
                "forbidden": r["forbidden_actions"],
            }
            for r in rows
        }
        for a, rows in res.items()
    }
    (results / "baselines_per_task.json").write_text(json.dumps(per_task, indent=2, sort_keys=True) + "\n")
    md = [
        f"# Baseline scores\n\nProduced by `{meta['command']}` on {meta['date']} ({meta['machine']}). "
        f"{meta['episodes']} episodes in {meta['wall_seconds']}s wall time with {args.jobs} worker processes.\n",
        table(summaries),
        "\n## Resolved rate by family\n",
        family_table(summaries),
    ]
    (results / "baselines.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    from triagebench import mcp_server
    from triagebench.tasks import build

    if not build.is_built(args.seed, args.task):
        print(f"task {args.task} is not generated; run `triagebench generate`", file=sys.stderr)
        return 1
    mcp_server.serve(
        args.task, args.seed, Path(args.trace) if args.trace else None, Path(args.result) if args.result else None
    )
    return 0


def cmd_run_llm(args: argparse.Namespace) -> int:
    from triagebench import scoring
    from triagebench.agents.llm import SUPPORTED_MODELS, LLMAgent, make_client
    from triagebench.runner import run_episode
    from triagebench.tasks import build

    if args.model not in SUPPORTED_MODELS:
        print(f"model {args.model} has no price entry; cost will be reported as unknown", file=sys.stderr)
    ids = _task_ids(args.tasks)[: args.limit]
    build.generate(args.seed, ids, jobs=4, log=lambda s: None)
    out = Path(args.out) if args.out else Path("runs") / f"llm-{args.model}-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"running {args.model} on {len(ids)} task(s): {', '.join(ids)}")
    client = make_client()
    rows = []
    for tid in ids:
        agent = LLMAgent(client, args.model, max_turns=args.max_turns, effort=args.effort, fallbacks=args.fallbacks)
        r = run_episode(agent, tid, args.seed, out)
        r["usage"] = vars(agent.usage)
        rows.append(r)
        (out / f"{tid}.transcript.json").write_text(json.dumps(agent.transcript, indent=1, default=str) + "\n")
        print(
            f"  {tid:<22} resolved={r['resolved']} decision={r['decision']} calls={r['tool_calls']} "
            f"tokens in/out={r['input_tokens']}/{r['output_tokens']} cost=${r.get('cost_usd') or 0:.4f}"
        )
    (out / "scores.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    summary = scoring.aggregate(rows)
    costs = [r.get("cost_usd") for r in rows]
    summary["cost"] = {
        "total_usd": round(sum(c for c in costs if c is not None), 4),
        "episodes_with_unknown_cost": sum(1 for c in costs if c is None),
        "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
        "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(table({f"llm:{args.model}": summary}))
    print(
        f"tokens in/out: {summary['cost']['input_tokens']}/{summary['cost']['output_tokens']}, "
        f"cost ${summary['cost']['total_usd']:.4f}; results in {out}"
    )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from triagebench import validate

    report = validate.validate(args.seed, args.jobs, determinism=not args.skip_determinism)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(f"validation {'PASSED' if report['ok'] else 'FAILED'} in {report['seconds']}s")
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="triagebench", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, fn: Any, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--seed", type=int, default=DEFAULT_SEED)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("list", cmd_list, "list tasks")
    sp.add_argument("--json", action="store_true")
    sp = add("generate", cmd_generate, "generate (or refresh) the base world and task worlds")
    sp.add_argument("--jobs", type=int, default=4)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--tasks", nargs="*")
    sp = add("run", cmd_run, "run scripted agents over tasks")
    sp.add_argument(
        "--agent",
        action="append",
        required=True,
        help="oracle, always_rerun, always_escalate, always_close, fix_everything",
    )
    sp.add_argument("--tasks", nargs="*", help="task ids or family names")
    sp.add_argument("--jobs", type=int, default=4)
    sp.add_argument("--out")
    sp = add("score", cmd_score, "aggregate the scores of a run directory")
    sp.add_argument("run_dir")
    sp = add("baselines", cmd_baselines, "run every scripted agent on every task and write results/")
    sp.add_argument("--jobs", type=int, default=4)
    sp.add_argument("--runs", default="runs")
    sp.add_argument("--results", default="results")
    sp = add("serve-mcp", cmd_serve_mcp, "serve one episode's tools over MCP (stdio)")
    sp.add_argument("--task", required=True)
    sp.add_argument("--trace")
    sp.add_argument("--result", help="write the episode score here when it ends")
    sp = add("run-llm", cmd_run_llm, "run a Claude model through the tools (paid; needs ANTHROPIC_API_KEY)")
    sp.add_argument("--model", default="claude-opus-5-5")
    sp.add_argument("--limit", type=int, default=1, help="number of tasks to run (default 1)")
    sp.add_argument("--tasks", nargs="*")
    sp.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    sp.add_argument("--max-turns", type=int, default=40)
    sp.add_argument("--fallbacks", action="store_true", help="let the API re-run refused requests on a fallback model")
    sp.add_argument("--out")
    sp = add("validate", cmd_validate, "oracle, determinism, leakage, alert and incremental checks")
    sp.add_argument("--jobs", type=int, default=4)
    sp.add_argument("--skip-determinism", action="store_true")
    sp.add_argument("--report")
    args = p.parse_args(argv)
    rc: int = args.fn(args)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
