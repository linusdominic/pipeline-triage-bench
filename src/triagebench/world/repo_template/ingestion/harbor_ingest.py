"""Load landing-zone files into the raw schema.

One function per run: ``run(source, ds, ...)`` finds the delivery for a business day,
checks it against the source's contract (columns and trailer), and replaces that
partition of ``raw.<source>``. Branch snapshots are loaded by snapshot date instead.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import yaml


class IngestError(Exception):
    """Base class for ingestion failures."""


class MissingDeliveryError(IngestError):
    pass


class SchemaMismatchError(IngestError):
    pass


class TrailerMismatchError(IngestError):
    pass


def load_contract(contracts_dir: Path, source: str) -> dict[str, Any]:
    with open(contracts_dir / f"{source}.yml") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    rx = re.escape(pattern).replace(r"\{yyyymmdd\}", r"(?P<d>\d{8})").replace(r"\{n\}", r"(?P<n>\d+)")
    return re.compile("^" + rx + "$")


def find_delivery(landing: Path, contract: dict[str, Any], ds: date) -> Path | None:
    """The newest revision delivered for ds, or None."""
    directory = landing / Path(contract["delivery"]["directory"]).name
    if not directory.is_dir():
        return None
    patterns = [_pattern_regex(contract["delivery"]["file_pattern"])]
    if contract["delivery"].get("redelivery_pattern"):
        patterns.append(_pattern_regex(contract["delivery"]["redelivery_pattern"]))
    best: tuple[int, Path] | None = None
    for p in sorted(directory.iterdir()):
        for rx in patterns:
            m = rx.match(p.name)
            if m and m.group("d") == ds.strftime("%Y%m%d"):
                rev = int(m.groupdict().get("n") or 0)
                if best is None or rev > best[0]:
                    best = (rev, p)
    return best[1] if best else None


def read_delivery(path: Path, contract: dict[str, Any]) -> tuple[list[str], list[list[str]], list[str] | None]:
    with open(path, newline="") as f:
        lines = f.read().splitlines()
    header = next(csv.reader([lines[0]]))
    body = [ln for ln in lines[1:] if ln]
    trailer: list[str] | None = None
    if body and body[-1].startswith("TRL|"):
        trailer = body.pop().split("|")
    rows = list(csv.reader(body))
    return header, rows, trailer


def check_columns(header: list[str], contract: dict[str, Any]) -> list[int]:
    expected = [c["name"] for c in contract["columns"]]
    missing = [c for c in expected if c not in header]
    unexpected = [c for c in header if c not in expected]
    if missing:
        raise SchemaMismatchError(
            f"file header does not match contract {contract['contract']} v{contract['version']}: "
            f"missing columns {missing}, unexpected columns {unexpected}"
        )
    return [header.index(c) for c in expected]


def check_trailer(rows: list[list[str]], trailer: list[str] | None, contract: dict[str, Any], name: str) -> None:
    if str(contract["format"].get("trailer", "none")).lower() == "none":
        return
    if trailer is None:
        raise TrailerMismatchError(f"{name}: trailer record missing; the file may be truncated")
    expected = int(trailer[1])
    if expected != len(rows):
        raise TrailerMismatchError(
            f"{name}: trailer says {expected} records, file contains {len(rows)}; the file may be truncated"
        )


def _replace_partition(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    partition_col: str,
    partition: date,
    columns: list[str],
    rows: list[list[str]],
    extra: dict[str, Any],
) -> None:
    conn.execute(f"delete from {table} where {partition_col} = ?", [partition])
    if not rows:
        return
    names = [*columns, "_row", *list(extra)]
    placeholders = ", ".join(["?"] * len(names))
    values = [[v if v != "" else None for v in r] + [i + 1] + list(extra.values()) for i, r in enumerate(rows)]
    conn.executemany(f"insert into {table} ({', '.join(names)}) values ({placeholders})", values)


def run(
    source: str,
    ds: date,
    *,
    landing_dir: Path,
    contracts_dir: Path,
    warehouse: Path,
    run_time: str,
    log: Callable[[str], None],
) -> dict[str, Any]:
    contract = load_contract(contracts_dir, source)
    table = f"raw.{source}"
    if source == "branches":
        return _run_branches(ds, contract, landing_dir, warehouse, run_time, log)

    path = find_delivery(landing_dir, contract, ds)
    if path is None:
        directory = contract["delivery"]["directory"]
        raise MissingDeliveryError(f"no delivery for {ds.isoformat()} in {directory}")
    log(f"Reading {contract['delivery']['directory']}/{path.name}")
    header, rows, trailer = read_delivery(path, contract)
    idx = check_columns(header, contract)
    check_trailer(rows, trailer, contract, path.name)
    if trailer is not None:
        log(f"Trailer OK: {trailer[1]} records, control total {trailer[2]}")
    columns = [c["name"] for c in contract["columns"]]
    ordered = [[r[i] for i in idx] for r in rows]
    conn = duckdb.connect(str(warehouse))
    try:
        conn.execute("begin")
        _replace_partition(
            conn,
            table,
            "_ds",
            ds,
            columns,
            ordered,
            {"_ds": ds, "_file": path.name, "_loaded_at": run_time},
        )
        conn.execute("commit")
    finally:
        conn.close()
    log(f"Loaded {len(ordered)} rows into {table} partition _ds={ds.isoformat()}")
    return {"rows": len(ordered), "file": path.name}


def _run_branches(
    ds: date,
    contract: dict[str, Any],
    landing_dir: Path,
    warehouse: Path,
    run_time: str,
    log: Callable[[str], None],
) -> dict[str, Any]:
    directory = landing_dir / Path(contract["delivery"]["directory"]).name
    rx = _pattern_regex(contract["delivery"]["file_pattern"])
    candidates: list[tuple[date, Path]] = []
    for p in sorted(directory.iterdir()) if directory.is_dir() else []:
        m = rx.match(p.name)
        if m:
            d = date(int(m.group("d")[:4]), int(m.group("d")[4:6]), int(m.group("d")[6:]))
            if d <= ds:
                candidates.append((d, p))
    conn = duckdb.connect(str(warehouse))
    try:
        loaded = {r[0] for r in conn.execute("select distinct _snapshot_date from raw.branches").fetchall()}
        new = [(d, p) for d, p in candidates if d not in loaded]
        if not new:
            latest = max(loaded) if loaded else None
            log(f"No new branch snapshot on or before {ds.isoformat()}; latest loaded snapshot is {latest}")
            return {"rows": 0, "file": None}
        snap, path = max(new)
        header, rows, _trailer = read_delivery(path, contract)
        idx = check_columns(header, contract)
        columns = [c["name"] for c in contract["columns"]]
        ordered = [[r[i] for i in idx] for r in rows]
        conn.execute("begin")
        _replace_partition(
            conn,
            "raw.branches",
            "_snapshot_date",
            snap,
            columns,
            ordered,
            {"_snapshot_date": snap, "_file": path.name, "_loaded_at": run_time},
        )
        conn.execute("commit")
    finally:
        conn.close()
    log(f"Loaded branch snapshot {snap.isoformat()} ({len(ordered)} branches) from {path.name}")
    return {"rows": len(ordered), "file": path.name}
