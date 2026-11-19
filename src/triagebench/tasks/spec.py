"""Task specification: the injected condition, the expected decision and the end state.

Nothing in this module is ever copied into an episode directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

DECISIONS = (
    "no_action",
    "rerun",
    "backfill",
    "fix_code",
    "fix_config",
    "escalate",
    "quarantine_and_escalate",
)

SOURCES = ("loans", "payments", "bureau", "branches", "calendar")
MODELS = (
    "stg_loans",
    "stg_payments",
    "stg_bureau",
    "dim_branch",
    "fct_payments",
    "fct_loan_balance_daily",
    "mart_delinquency",
)
MONITORS = (
    "freshness_raw_payments",
    "freshness_raw_bureau",
    "freshness_mart_delinquency",
    "volume_raw_payments",
    "volume_raw_loans",
    "volume_fct_payments",
    "schema_payments",
    "schema_bureau",
    "schema_loans",
    "reconciliation_payments",
    "dbt_tests",
    "task_failures",
)


def root_cause_vocabulary() -> list[str]:
    return (
        [f"source:{s}" for s in SOURCES]
        + [f"model:{m}" for m in MODELS]
        + [f"monitor:{m}" for m in MONITORS]
        + ["orchestration", "infrastructure"]
    )


# --- conditions applied to a world ---------------------------------------------------------


@dataclass(frozen=True)
class LateFile:
    source: str
    ds: date
    arrival: datetime


@dataclass(frozen=True)
class TaskFailure:
    task: str
    ds: date
    attempts: int
    error: str
    detail: tuple[str, ...] = ()


@dataclass(frozen=True)
class BadCommit:
    path: str
    edits: tuple[tuple[str, str], ...]  # (find, replace) pairs applied in order
    at: datetime
    author: str
    message: str


@dataclass(frozen=True)
class ConfigCommit:
    monitor: str
    param: str
    value: Any
    at: datetime
    author: str
    message: str


@dataclass(frozen=True)
class Cents:
    ds: date
    fraction: float


@dataclass(frozen=True)
class Rename:
    source: str
    ds: date
    old: str
    new: str


@dataclass(frozen=True)
class Truncate:
    source: str
    ds: date
    keep_fraction: float
    redelivery_at: datetime | None


@dataclass(frozen=True)
class CdcReplay:
    ds: date
    fraction: float


@dataclass(frozen=True)
class Discontinue:
    products: tuple[str, ...]
    effective: date
    announced: date
    text: str


@dataclass(frozen=True)
class Announce:
    on: date
    author: str
    text: str


Condition = (
    LateFile | TaskFailure | BadCommit | ConfigCommit | Cents | Rename | Truncate | CdcReplay | Discontinue | Announce
)


# --- scoring rules ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """Matches a write action. Each constraint restricts one argument.

    A list constraint means the argument (or, for a list argument, every element) must be
    in the list. A dict constraint on `params` maps parameter names to allowed values or
    to a (min, max) range. Strings may contain placeholders such as {bad_sha}.
    """

    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MonitorRequirement:
    monitor: str
    param: str
    minimum: float
    maximum: float


@dataclass(frozen=True)
class TaskSpec:
    id: str
    family: str
    variant: str
    summary: str
    incident_ds: date
    window_start: date
    decision: str
    root_cause: str
    expected_alerts: frozenset[str]
    reference_mode: str  # clean | initial | quarantine
    faults: tuple[Condition, ...] = ()
    benign: tuple[Condition, ...] = ()
    root_cause_alternates: tuple[str, ...] = ()
    acceptable: tuple[Rule, ...] = ()
    forbidden: tuple[Rule, ...] = ()
    required_escalation: str | None = None
    required_quarantine: tuple[tuple[str, str], ...] = ()
    monitor_requirements: tuple[MonitorRequirement, ...] = ()
    allowed_monitor_changes: tuple[str, ...] = ()
    affected_partitions: tuple[str, ...] = ()
    oracle: tuple[tuple[str, dict[str, Any]], ...] = ()
    noise: str | None = None

    def __post_init__(self) -> None:
        assert self.decision in DECISIONS, self.decision
        vocab = set(root_cause_vocabulary())
        assert self.root_cause in vocab, self.root_cause
        for alt in self.root_cause_alternates:
            assert alt in vocab, alt
        assert self.reference_mode in ("clean", "initial", "quarantine")
