"""Content fingerprints of a world: warehouse tables per partition, landing files, monitors.

Lineage columns written at load time (`_file`, `_loaded_at`) are excluded, so a partition
loaded later or from a re-delivered file with the same rows fingerprints the same.
"""

from __future__ import annotations

import hashlib
from typing import Any

import yaml

from triagebench.world.world import World

EXCLUDED_COLUMNS = {"_file", "_loaded_at"}
# Free text and timestamps written by whoever quarantined a partition.
EXCLUDED_BY_TABLE = {"ops.quarantine": {"reason", "quarantined_at"}}
PARTITION_COLUMNS = ("ds", "_ds", "_snapshot_date")
SCHEMAS = ("raw", "main", "ops")
OPS_TABLES = {"quarantine"}  # process metadata in ops (test results, publish log) is not state


def tables(world: World) -> dict[str, dict[str, Any]]:
    """Fingerprint every data table: {schema.table: {"columns": [...], "partitions": {key: [rows, md5]}}}."""
    out: dict[str, dict[str, Any]] = {}
    conn = world.connect(read_only=True)
    try:
        rows = conn.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema in ('raw', 'main', 'ops') order by 1, 2"
        ).fetchall()
        for schema, name in rows:
            if schema == "ops" and name not in OPS_TABLES:
                continue
            fq = f"{schema}.{name}"
            cols = [
                (c[0], c[1])
                for c in conn.execute(
                    "select column_name, data_type from information_schema.columns "
                    "where table_schema = ? and table_name = ? order by ordinal_position",
                    [schema, name],
                ).fetchall()
            ]
            skip = EXCLUDED_COLUMNS | EXCLUDED_BY_TABLE.get(fq, set())
            names = [c for c, _ in cols if c not in skip]
            pcol = next((p for p in PARTITION_COLUMNS if p in names), None)
            if not names:
                out[fq] = {"columns": [], "partitions": {}}
                continue
            row_expr = "md5(cast(row(" + ", ".join(f'"{c}"' for c in names) + ") as varchar))"
            key_expr = f'cast("{pcol}" as varchar)' if pcol else "'*'"
            parts = conn.execute(
                f"select k, count(*), md5(string_agg(h, ',' order by h)) "
                f"from (select {key_expr} as k, {row_expr} as h from {fq}) t group by k order by k"
            ).fetchall()
            out[fq] = {
                "columns": [f"{c}:{t}" for c, t in cols if c not in skip],
                "partition_column": pcol,
                "partitions": {str(k): [int(n), str(h)] for k, n, h in parts},
            }
    finally:
        conn.close()
    return out


def landing(world: World) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(world.landing.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(world.landing))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def monitors(world: World) -> dict[str, Any]:
    cfg = yaml.safe_load((world.repo / "monitoring" / "monitors.yml").read_text())
    return {
        "defaults": cfg.get("defaults", {}),
        "monitors": {m["name"]: m for m in cfg.get("monitors", [])},
    }


def world_fingerprint(world: World) -> dict[str, Any]:
    return {"tables": tables(world), "landing": landing(world), "monitors": monitors(world)}


def digest(obj: Any) -> str:
    import json

    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()
