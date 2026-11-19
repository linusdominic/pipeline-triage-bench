"""Build task worlds from the cached base world, and their hidden ground truth.

For each task two worlds are produced from the same rewound copy of the base world:

* the snapshot, with the fault (and any benign conditions) applied while the scheduler runs
  the incident window; this is what an episode starts from;
* the reference, the expected end state: the same window run without the fault
  (`clean`), the snapshot itself (`initial`, nothing should change), or the snapshot with
  the required quarantine applied (`quarantine`).

The ground truth (spec, fingerprints, commit SHAs) is written next to the snapshot, never
inside it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

from triagebench import fingerprint
from triagebench.config import seed_dir
from triagebench.tasks import catalog
from triagebench.tasks.spec import (
    Announce,
    BadCommit,
    CdcReplay,
    Cents,
    Condition,
    ConfigCommit,
    Discontinue,
    LateFile,
    Rename,
    TaskFailure,
    TaskSpec,
    Truncate,
)
from triagebench.world import actions, datagen, lifecycle, monitors, repo
from triagebench.world import calendar as cal
from triagebench.world.datagen import LandingFile, Timeline, cents, dollars, trailer_for
from triagebench.world.orchestrator import FailureHook, Hooks, Orchestrator
from triagebench.world.world import World


def _template_digest() -> str:
    """Hash of the files every world's git repo is created from (dbt project, runbooks...).

    Part of BUILD_VERSION, so that editing the template rebuilds cached worlds instead of
    leaving them with the old files.
    """
    root = Path(repo.__file__).parent / "repo_template"
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:12]


BUILD_VERSION = f"5-{_template_digest()}"


def base_dir(seed: int) -> Path:
    return seed_dir(seed) / "base"


def task_dir(seed: int, task_id: str) -> Path:
    return seed_dir(seed) / "tasks" / task_id


def ensure_base(seed: int, force: bool = False) -> World:
    root = base_dir(seed) / "world"
    marker = base_dir(seed) / "ok.json"
    if marker.exists() and not force and json.loads(marker.read_text()).get("version") == BUILD_VERSION:
        return World(root)
    t0 = time.time()
    lifecycle.build_base(seed, root)
    marker.write_text(json.dumps({"version": BUILD_VERSION, "seconds": round(time.time() - t0, 1)}) + "\n")
    return World(root)


# --- file transforms --------------------------------------------------------------------


def _copy(f: LandingFile) -> LandingFile:
    return dataclasses.replace(f, header=list(f.header), rows=[list(r) for r in f.rows])


def _x100(v: str) -> str:
    return dollars(cents(float(v)) * 100)


def apply_file_conditions(
    tl: Timeline, conds: tuple[Condition, ...], window: tuple[date, date], cutoff: datetime, end: datetime
) -> list[LandingFile]:
    files = {k: _copy(f) for k, f in tl.files.items()}
    extra: list[LandingFile] = []
    for c in conds:
        if isinstance(c, LateFile):
            files[(c.source, c.ds)].arrived_at = c.arrival
        elif isinstance(c, Cents):
            f = files[("payments", c.ds)]
            for r in f.rows:
                h = int(hashlib.sha256(r[0].encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
                if h < c.fraction:
                    r[3], r[4], r[5] = _x100(r[3]), _x100(r[4]), _x100(r[5])
        elif isinstance(c, Rename):
            f = files[(c.source, c.ds)]
            f.header[f.header.index(c.old)] = c.new
        elif isinstance(c, Truncate):
            f = files[(c.source, c.ds)]
            full = _copy(f)
            f.rows = f.rows[: int(len(f.rows) * c.keep_fraction)]
            if c.redelivery_at is not None:
                stem = full.name.removesuffix(".csv")
                extra.append(dataclasses.replace(full, name=f"{stem}_r1.csv", arrived_at=c.redelivery_at))
        elif isinstance(c, CdcReplay):
            f = files[("payments", c.ds)]
            k = int(len(f.rows) * c.fraction)
            f.rows = f.rows + [list(r) for r in f.rows[len(f.rows) - k :]]
            f.trailer = trailer_for(f.rows, 3)
        elif isinstance(c, Discontinue):
            for (src, ds), f in files.items():
                if src == "loans" and ds >= c.effective:
                    f.rows = [r for r in f.rows if r[3] not in c.products]
                    f.trailer = trailer_for(f.rows, 5)
    out = [f for f in list(files.values()) + extra if cutoff < f.arrived_at <= end]
    return out


# --- running a window ------------------------------------------------------------------------


def _announcements(tl: Timeline, conds: tuple[Condition, ...]) -> list[lifecycle.Announcement]:
    ann = lifecycle.base_announcements(tl)
    for c in conds:
        if isinstance(c, Discontinue):
            ann.append(lifecycle.Announcement(c.announced, "Product management", c.text))
        elif isinstance(c, Announce):
            ann.append(lifecycle.Announcement(c.on, c.author, c.text))
    return ann


def _commit_condition(world: World, c: BadCommit | ConfigCommit) -> str:
    if isinstance(c, BadCommit):
        path = world.repo / c.path
        text = path.read_text()
        for find, replace in c.edits:
            if find not in text:
                raise RuntimeError(f"bad commit edit does not apply to {c.path}: {find!r}")
            text = text.replace(find, replace, 1)
        path.write_text(text)
    else:
        path = world.repo / "monitoring" / "monitors.yml"
        path.write_text(actions.set_monitor_params(path.read_text(), c.monitor, {c.param: c.value}))
    return repo.commit_all(world.repo, c.message, c.at, c.author)


def run_window(world: World, tl: Timeline, spec: TaskSpec, with_faults: bool, with_monitors: bool) -> dict[str, str]:
    conds: tuple[Condition, ...] = spec.benign + (spec.faults if with_faults else ())
    rewind_to = cal.prev_business_day(spec.window_start)
    lifecycle.rewind(world, rewind_to)
    cutoff = cal.run_time_for(rewind_to)
    end = cal.check_time_for(spec.incident_ds)
    files = apply_file_conditions(tl, conds, (spec.window_start, spec.incident_ds), cutoff, end)
    hooks = Hooks()
    for c in conds:
        if isinstance(c, TaskFailure):
            hooks.failures[(c.task, c.ds.isoformat())] = FailureHook(c.attempts, c.error, c.detail)
    events: list[tuple[datetime, int, str, Any]] = []
    for f in files:
        events.append((f.arrived_at, 0, "deliver", f))
    for c in conds:
        if isinstance(c, BadCommit | ConfigCommit):
            events.append((c.at, 1, "commit", c))
    for ds in cal.business_days(spec.window_start, spec.incident_ds):
        events.append((cal.run_time_for(ds), 2, "run", ds))
        if with_monitors:
            events.append((datetime.combine(ds + timedelta(days=1), dtime(7, 30)), 3, "monitors", ds))
    events.sort(key=lambda e: (e[0], e[1], str(e[3]) if e[2] != "deliver" else e[3].name))
    orch = Orchestrator(world, hooks)
    shas: dict[str, str] = {}
    for when, _, kind, payload in events:
        if kind == "deliver":
            lifecycle.deliver(world, payload)
        elif kind == "commit":
            sha = _commit_condition(world, payload)
            shas["bad_sha" if isinstance(payload, BadCommit) else "config_sha"] = sha
        elif kind == "run":
            orch.run_dag(payload, start=when)
        elif kind == "monitors":
            world.set_now(when)
            monitors.run(world)
    world.set_now(end)
    if with_monitors:
        monitors.run(world)
    lifecycle.write_reference(world, _announcements(tl, conds))
    return shas


def _spec_json(spec: TaskSpec) -> dict[str, Any]:
    def conv(o: Any) -> Any:
        if isinstance(o, date | datetime):
            return o.isoformat()
        if isinstance(o, frozenset | set):
            return sorted(o)
        if isinstance(o, tuple | list):
            return [conv(x) for x in o]
        if isinstance(o, dict):
            return {k: conv(v) for k, v in o.items()}
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            d = {f.name: conv(getattr(o, f.name)) for f in dataclasses.fields(o)}
            d["kind"] = type(o).__name__
            return d
        return o

    result: dict[str, Any] = conv(spec)
    return result


def build_task(seed: int, task_id: str) -> dict[str, Any]:
    t0 = time.time()
    spec = catalog.get(task_id)
    base = World(base_dir(seed) / "world")
    out = task_dir(seed, task_id)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    tl = datagen.generate(seed)

    snap = base.copy_to(out / "world")
    shas = run_window(snap, tl, spec, with_faults=True, with_monitors=True)
    initial = fingerprint.world_fingerprint(snap)
    alerts = [a for a in snap.alerts() if a["status"] == "open"]

    if spec.reference_mode == "initial":
        reference_tables = initial["tables"]
    else:
        ref = (
            snap.copy_to(out / "reference") if spec.reference_mode == "quarantine" else base.copy_to(out / "reference")
        )
        if spec.reference_mode == "clean":
            run_window(ref, tl, spec, with_faults=False, with_monitors=False)
        else:
            for table, partition in spec.required_quarantine:
                actions.quarantine(ref, table, partition, "reference")
        reference_tables = fingerprint.tables(ref)
        shutil.rmtree(out / "reference")

    fired = sorted({a["monitor"] for a in alerts})
    gt = {
        "version": BUILD_VERSION,
        "seed": seed,
        "task": _spec_json(spec),
        "placeholders": shas,
        "alerts": [{"id": a["id"], "monitor": a["monitor"], "partition": a.get("partition")} for a in alerts],
        "alert_check": {
            "expected": sorted(spec.expected_alerts),
            "fired": fired,
            "ok": set(fired) == set(spec.expected_alerts),
        },
        "initial": initial,
        "reference_tables": reference_tables,
        "build_seconds": round(time.time() - t0, 1),
    }
    (out / "ground_truth.json").write_text(json.dumps(gt, indent=1, sort_keys=True) + "\n")
    return {
        "task": task_id,
        "alerts_ok": gt["alert_check"]["ok"],
        "fired": fired,
        "expected": sorted(spec.expected_alerts),
        "seconds": gt["build_seconds"],
    }


def _build_task_entry(args: tuple[int, str]) -> dict[str, Any]:
    return build_task(*args)


def generate(
    seed: int, task_ids: list[str] | None = None, jobs: int = 4, force: bool = False, log: Any = print
) -> list[dict[str, Any]]:
    t0 = time.time()
    ensure_base(seed, force=force)
    log(f"base world ready ({time.time() - t0:.1f}s)")
    ids = task_ids or [t.id for t in catalog.all_tasks()]
    todo = [i for i in ids if force or not is_built(seed, i)]
    results: list[dict[str, Any]] = []
    if jobs <= 1:
        for i in todo:
            results.append(build_task(seed, i))
            log(_fmt(results[-1]))
    else:
        import multiprocessing as mp

        with ProcessPoolExecutor(max_workers=jobs, mp_context=mp.get_context("spawn")) as ex:
            for r in ex.map(_build_task_entry, [(seed, i) for i in todo]):
                results.append(r)
                log(_fmt(r))
    log(f"generated {len(todo)} task(s) in {time.time() - t0:.1f}s")
    return results


def _fmt(r: dict[str, Any]) -> str:
    flag = "ok" if r["alerts_ok"] else f"ALERT MISMATCH expected={r['expected']} fired={r['fired']}"
    return f"  {r['task']:<22} {r['seconds']:>5.1f}s  {flag}"


def is_built(seed: int, task_id: str) -> bool:
    gt = task_dir(seed, task_id) / "ground_truth.json"
    if not gt.exists():
        return False
    try:
        data = json.loads(gt.read_text())
    except json.JSONDecodeError:
        return False
    # Rebuild when the build code version or the task's specification changed.
    return bool(data.get("version") == BUILD_VERSION and data.get("task") == _spec_json(catalog.get(task_id)))


def load_ground_truth(seed: int, task_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((task_dir(seed, task_id) / "ground_truth.json").read_text())
    return data
