"""Fixed parameters of the Harbor Lending world and where generated files live."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

DEFAULT_SEED = 7

# The world's timeline. Business-day partitions from WORLD_START to WORLD_END.
BACKBOOK_DATE = date(2026, 7, 31)  # one-off migration extract of the existing loan book
WORLD_START = date(2026, 8, 3)
WORLD_END = date(2026, 11, 13)
# Partitions up to and including this date are built by one historical full-refresh run;
# later partitions are built one day at a time by the scheduler, as in production.
CUTOVER_DATE = date(2026, 8, 28)

BANK_HOLIDAYS = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 19): "Martin Luther King Jr. Day",
    date(2026, 2, 16): "Presidents Day",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth",
    date(2026, 7, 3): "Independence Day (observed)",
    date(2026, 9, 7): "Labor Day",
    date(2026, 10, 12): "Columbus Day",
    date(2026, 11, 11): "Veterans Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
}

# Branch openings scheduled in the world. Tasks about stale reference data rely on these.
BRANCH_OPENINGS = {
    "BR-031": date(2026, 8, 19),
    "BR-032": date(2026, 9, 16),
    "BR-033": date(2026, 10, 6),
    "BR-034": date(2026, 10, 7),
    "BR-035": date(2026, 10, 21),
    "BR-036": date(2026, 11, 3),
}

EPISODE_CHECK_TIME = "08:30"  # on-call looks at alerts at this time on the day after the incident
DAG_RUN_TIME = "06:00"
MONITOR_RUN_TIME = "07:30"


def cache_root() -> Path:
    """Generated worlds and episode scratch space. Always inside the working directory."""
    env = os.environ.get("TRIAGEBENCH_CACHE")
    return Path(env).resolve() if env else (Path.cwd() / ".cache" / "triagebench").resolve()


def seed_dir(seed: int) -> Path:
    return cache_root() / f"seed-{seed}"


def episodes_dir() -> Path:
    return cache_root() / "episodes"
