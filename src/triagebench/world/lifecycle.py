"""Creating, advancing and rewinding worlds."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from triagebench.config import BACKBOOK_DATE, BANK_HOLIDAYS, BRANCH_OPENINGS, CUTOVER_DATE, WORLD_END
from triagebench.world import calendar as cal
from triagebench.world import datagen, repo
from triagebench.world.calendar import fmt_ts
from triagebench.world.datagen import LandingFile, Timeline
from triagebench.world.orchestrator import Orchestrator, dag_for
from triagebench.world.world import World


@dataclass(frozen=True)
class Announcement:
    on: date
    author: str
    text: str


def base_announcements(tl: Timeline) -> list[Announcement]:
    names = {b.code: b.name for b in tl.branches}
    out = [
        Announcement(
            date(2026, 7, 20),
            "Data platform",
            "harbor_daily moves to the new scheduler with the 2026-08-31 run. Partitions up to "
            "2026-08-28 are loaded by a one-off full-refresh dbt build.",
        ),
        Announcement(
            date(2026, 8, 24),
            "Keel Servicing",
            "Scheduled maintenance on Saturday 2026-09-12, 02:00-04:00 ET. No change to file delivery.",
        ),
        Announcement(
            date(2026, 9, 28),
            "Finance reporting",
            "September month-end close runs 2026-10-01 to 2026-10-05. Please avoid changes to "
            "mart_delinquency in that window.",
        ),
        Announcement(
            date(2026, 10, 26),
            "Meridian Credit Bureau",
            "Refresh files move to SFTP host sftp2.meridianbureau.example from 2026-12-07. "
            "No change to file names or format.",
        ),
    ]
    notice = {
        "BR-031": date(2026, 8, 3),
        "BR-032": date(2026, 9, 1),
        "BR-033": date(2026, 9, 22),
        "BR-034": date(2026, 9, 22),
        "BR-035": date(2026, 10, 7),
        "BR-036": date(2026, 10, 19),
    }
    for code, opened in sorted(BRANCH_OPENINGS.items()):
        out.append(
            Announcement(
                notice[code],
                "Branch operations",
                f"{names[code]} ({code}) opens on {opened.isoformat()}. It will be in the "
                f"branch snapshot for the week it opens.",
            )
        )
    return out


def write_reference(world: World, announcements: list[Announcement]) -> None:
    world.reference.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Harbor Lending business calendar 2026",
        "# Business days are Monday to Friday except the bank holidays below.",
        "bank_holidays:",
    ]
    for d, name in sorted(BANK_HOLIDAYS.items()):
        if d.year == 2026:
            lines.append(f'  - {{date: {d.isoformat()}, name: "{name}"}}')
    (world.reference / "business_calendar.yaml").write_text("\n".join(lines) + "\n")
    now = world.now().date()
    visible = sorted((a for a in announcements if a.on <= now), key=lambda a: (a.on, a.author, a.text))
    md = ["# Announcements", "", "Newest last. Posted to #data-announcements and copied here.", ""]
    for a in visible:
        md.append(f"- **{a.on.isoformat()}, {a.author}:** {a.text}")
    (world.reference / "announcements.md").write_text("\n".join(md) + "\n")


def create_empty(root: Path, seed: int) -> World:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    world = World(root)
    world.save_state({"now": fmt_ts(datetime(2026, 7, 31, 0, 0)), "run_counter": 0, "seed": seed})
    for d in ("landing", "warehouse", "orchestrator", "alerts", "outbox", "reference"):
        (root / d).mkdir()
    world.transfer_log.write_text("path,arrived_at,bytes\n")
    world.save_alerts([])
    world.outbox.write_text("")
    repo.build_history(world.repo)
    conn = duckdb.connect(str(world.warehouse))
    try:
        conn.execute((world.repo / "warehouse" / "ddl.sql").read_text())
    finally:
        conn.close()
    return world


class Deliveries:
    """Files waiting to arrive in the landing zone, in arrival order."""

    def __init__(self, files: list[LandingFile]) -> None:
        self.pending = sorted(files, key=lambda f: (f.arrived_at, f.source, f.name))

    def until(self, world: World, t: datetime) -> None:
        while self.pending and self.pending[0].arrived_at <= t:
            f = self.pending.pop(0)
            deliver(world, f)


def deliver(world: World, f: LandingFile) -> None:
    d = world.landing / f.source
    d.mkdir(parents=True, exist_ok=True)
    text = f.render()
    (d / f.name).write_text(text)
    with open(world.transfer_log, "a") as log:
        log.write(f"{f.source}/{f.name},{fmt_ts(f.arrived_at)},{len(text.encode())}\n")


def build_base(seed: int, root: Path) -> tuple[World, Timeline]:
    tl = datagen.generate(seed)
    world = create_empty(root, seed)
    orch = Orchestrator(world)
    deliveries = Deliveries(list(tl.files.values()))

    history = [BACKBOOK_DATE, *cal.business_days(date(2026, 8, 3), CUTOVER_DATE)]
    for ds in history:
        start = cal.run_time_for(ds)
        deliveries.until(world, start)
        world.set_now(start)
        for t in dag_for(ds):
            if t.name.startswith("ingest_"):
                orch.run_task(t, ds, "scheduled")
    world.set_now(cal.run_time_for(CUTOVER_DATE) + timedelta(hours=2))
    state = orch.full_refresh(CUTOVER_DATE, f"initial load: full refresh of {BACKBOOK_DATE}..{CUTOVER_DATE}")
    if state != "success":
        raise RuntimeError("historical full refresh failed; see orchestrator logs")

    for ds in cal.business_days(CUTOVER_DATE + timedelta(days=1), WORLD_END):
        start = cal.run_time_for(ds)
        deliveries.until(world, start)
        states = orch.run_dag(ds, start=start)
        if any(s != "success" for s in states.values()):
            raise RuntimeError(f"base world run failed for {ds}: {states}")
    end = cal.check_time_for(WORLD_END)
    deliveries.until(world, end)
    world.set_now(end)
    write_reference(world, base_announcements(tl))
    return world, tl


def rewind(world: World, to_ds: date) -> None:
    """Return the world to the state right after the DAG run for `to_ds`."""
    conn = world.connect()
    try:
        for t in ("raw.loans", "raw.payments", "raw.bureau"):
            conn.execute(f"delete from {t} where _ds > ?", [to_ds])
        conn.execute("delete from raw.branches where _snapshot_date > ?", [to_ds])
        models = [
            r[0]
            for r in conn.execute(
                "select table_name from information_schema.tables where table_schema = 'main'"
            ).fetchall()
        ]
        for m in models:
            col = "first_snapshot_date" if m == "dim_branch" else "ds"
            conn.execute(f"delete from main.{m} where {col} > ?", [to_ds])
        conn.execute("delete from ops.dbt_test_results where ds > ?", [to_ds])
        conn.execute("delete from ops.published_partitions where ds > ?", [to_ds])
        conn.execute("delete from ops.quarantine")
        conn.execute("checkpoint")
    finally:
        conn.close()
    runs = [r for r in world.task_runs() if date.fromisoformat(r["ds"]) <= to_ds]
    world.task_runs_file.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in runs))
    logs = world.orchestrator / "logs"
    for d in logs.iterdir():
        if date.fromisoformat(d.name.removeprefix("ds=")) > to_ds:
            shutil.rmtree(d)
    cutoff = cal.run_time_for(to_ds)
    keep = ["path,arrived_at,bytes"]
    for line in world.transfer_log.read_text().splitlines()[1:]:
        path, arrived, _ = line.split(",")
        if cal.parse_ts(arrived) <= cutoff:
            keep.append(line)
        else:
            (world.landing / path).unlink(missing_ok=True)
    world.transfer_log.write_text("\n".join(keep) + "\n")
    world.save_alerts([])
    world.outbox.write_text("")
    st = world.state()
    st["run_counter"] = max([int(r["run_id"].split("-")[1]) for r in runs] or [0])
    st["now"] = fmt_ts(cutoff + timedelta(hours=1))
    world.save_state(st)
