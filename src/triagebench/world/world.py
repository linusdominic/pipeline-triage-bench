"""A world directory: landing zone, warehouse, pipeline repo, orchestrator records, alerts.

Layout (all paths relative to the world root)::

    landing/<source>/...          files as delivered, plus landing/_transfer_log.csv
    warehouse/harbor.duckdb       the warehouse
    repo/                         git repository: ingestion, dbt, contracts, monitors, runbooks
    orchestrator/task_runs.jsonl  one record per task attempt
    orchestrator/logs/...         task logs
    alerts/alerts.json            alerts raised by the monitors
    reference/                    business calendar and announcements
    outbox/escalations.jsonl      messages sent to owners
    state.json                    the simulated clock
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from triagebench.world.calendar import fmt_ts, parse_ts

WORLD_MARKER = "state.json"


class World:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    # --- paths -------------------------------------------------------------------------
    @property
    def landing(self) -> Path:
        return self.root / "landing"

    @property
    def warehouse(self) -> Path:
        return self.root / "warehouse" / "harbor.duckdb"

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def dbt_dir(self) -> Path:
        return self.repo / "dbt"

    @property
    def orchestrator(self) -> Path:
        return self.root / "orchestrator"

    @property
    def task_runs_file(self) -> Path:
        return self.orchestrator / "task_runs.jsonl"

    @property
    def alerts_file(self) -> Path:
        return self.root / "alerts" / "alerts.json"

    @property
    def reference(self) -> Path:
        return self.root / "reference"

    @property
    def outbox(self) -> Path:
        return self.root / "outbox" / "escalations.jsonl"

    @property
    def state_file(self) -> Path:
        return self.root / WORLD_MARKER

    @property
    def transfer_log(self) -> Path:
        return self.landing / "_transfer_log.csv"

    # --- state -------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.state_file.read_text())
        return data

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    def now(self) -> datetime:
        return parse_ts(self.state()["now"])

    def set_now(self, ts: datetime) -> None:
        st = self.state()
        st["now"] = fmt_ts(ts)
        self.save_state(st)

    def advance(self, seconds: float) -> datetime:
        from datetime import timedelta

        ts = self.now() + timedelta(seconds=round(seconds))
        self.set_now(ts)
        return ts

    def next_run_id(self) -> str:
        st = self.state()
        n = int(st.get("run_counter", 0)) + 1
        st["run_counter"] = n
        self.save_state(st)
        return f"tr-{n:06d}"

    # --- warehouse ---------------------------------------------------------------------
    def connect(self, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.warehouse), read_only=read_only)

    def connect_sandboxed(self) -> duckdb.DuckDBPyConnection:
        """Read-only connection with file-system access disabled, for agent queries."""
        conn = duckdb.connect(
            str(self.warehouse),
            read_only=True,
            config={"enable_external_access": False, "autoload_known_extensions": False},
        )
        conn.execute("set lock_configuration = true")
        return conn

    # --- records -----------------------------------------------------------------------
    def task_runs(self) -> list[dict[str, Any]]:
        if not self.task_runs_file.exists():
            return []
        return [json.loads(x) for x in self.task_runs_file.read_text().splitlines() if x.strip()]

    def append_task_run(self, rec: dict[str, Any]) -> None:
        self.task_runs_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.task_runs_file, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    def alerts(self) -> list[dict[str, Any]]:
        if not self.alerts_file.exists():
            return []
        data: list[dict[str, Any]] = json.loads(self.alerts_file.read_text())
        return data

    def save_alerts(self, alerts: list[dict[str, Any]]) -> None:
        self.alerts_file.parent.mkdir(parents=True, exist_ok=True)
        self.alerts_file.write_text(json.dumps(alerts, indent=2, sort_keys=True) + "\n")

    def escalations(self) -> list[dict[str, Any]]:
        if not self.outbox.exists():
            return []
        return [json.loads(x) for x in self.outbox.read_text().splitlines() if x.strip()]

    def copy_to(self, dest: Path) -> World:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.root, dest, symlinks=False)
        return World(dest)


def is_world_dir(path: Path) -> bool:
    return (path / WORLD_MARKER).is_file()
