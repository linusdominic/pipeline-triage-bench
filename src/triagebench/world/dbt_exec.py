"""Run dbt in-process against a world's warehouse and pipeline repo."""

from __future__ import annotations

import contextlib
import io
import json
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from triagebench.world.world import World

os.environ.setdefault("DO_NOT_TRACK", "1")
os.environ.setdefault("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")


@dataclass
class NodeResult:
    unique_id: str
    name: str
    resource_type: str
    status: str
    message: str
    failures: int | None
    model: str | None


@dataclass
class DbtResult:
    success: bool
    nodes: list[NodeResult] = field(default_factory=list)
    error: str | None = None


def sanitize(text: str, world: World) -> str:
    """Remove host paths from dbt messages so outputs are portable and deterministic."""
    return text.replace(str(world.root) + "/", "").replace(str(world.root), ".")


def _close_duckdb() -> None:
    try:
        from dbt.adapters.duckdb.connections import DuckDBConnectionManager

        close: Any = DuckDBConnectionManager.close_all_connections
        close()
    except Exception:  # pragma: no cover - best effort
        pass


def run(
    world: World, command: str, ds: date | None, select: str | None = None, full_refresh: bool = False
) -> DbtResult:
    from dbt.cli.main import dbtRunner

    os.environ["HARBOR_WAREHOUSE"] = str(world.warehouse)
    args = [
        command,
        "--project-dir",
        str(world.dbt_dir),
        "--profiles-dir",
        str(world.dbt_dir),
        "--log-level",
        "none",
        "--log-level-file",
        "none",
        "--no-send-anonymous-usage-stats",
        "--no-use-colors",
    ]
    if command in ("build", "run", "test", "compile"):
        args += ["--vars", json.dumps({"ds": ds.isoformat() if ds else None})]
    if select:
        args += ["--select", select]
    if full_refresh:
        args += ["--full-refresh"]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            res = dbtRunner().invoke(args)
    finally:
        _close_duckdb()
    if res.exception is not None:
        return DbtResult(False, [], sanitize(f"{type(res.exception).__name__}: {res.exception}", world))
    nodes: list[NodeResult] = []
    result: Any = res.result
    for r in getattr(result, "results", []) or []:
        node = r.node
        depends = getattr(getattr(node, "depends_on", None), "nodes", []) or []
        model = None
        if node.resource_type == "test":
            refs = [d.split(".")[-1] for d in depends if d.startswith("model.")]
            model = refs[0] if refs else None
        nodes.append(
            NodeResult(
                unique_id=node.unique_id,
                name=node.name,
                resource_type=str(node.resource_type),
                status=str(r.status),
                message=sanitize(str(r.message or ""), world),
                failures=r.failures,
                model=model,
            )
        )
    return DbtResult(bool(res.success), nodes, None)


def manifest(world: World) -> dict[str, Any] | None:
    path = world.dbt_dir / "target" / "manifest.json"
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    return data
