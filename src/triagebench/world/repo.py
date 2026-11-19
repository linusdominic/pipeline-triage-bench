"""The pipeline git repository inside a world, built with a fixed, deterministic history."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from triagebench.world.world import is_world_dir

TEMPLATE = Path(__file__).parent / "repo_template"

AUTHORS = {
    "priya": ("Priya Natarajan", "priya.natarajan@harborlending.example"),
    "dana": ("Dana Okafor", "dana.okafor@harborlending.example"),
    "tom": ("Tom Becker", "tom.becker@harborlending.example"),
    "sam": ("Sam Reyes", "sam.reyes@harborlending.example"),
    "oncall": ("Data platform on-call", "data-platform@harborlending.example"),
}


class GitError(Exception):
    pass


def _check_repo_path(repo: Path) -> None:
    # Only ever operate on a repository inside a world directory.
    if not is_world_dir(repo.parent):
        raise GitError(f"refusing to run git outside a world directory: {repo}")


def git(repo: Path, *args: str, when: datetime | None = None, author: str = "oncall", stdin: str | None = None) -> str:
    _check_repo_path(repo)
    name, email = AUTHORS[author]
    stamp = (when or datetime(2026, 1, 1)).strftime("%Y-%m-%dT%H:%M:%S-05:00")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(repo),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
        "TZ": "UTC",
        "LC_ALL": "C",
    }
    cmd = [
        "git",
        "-C",
        str(repo),
        "-c",
        "core.autocrlf=false",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "advice.detachedHead=false",
        *args,
    ]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, input=stdin, check=False)
    if res.returncode != 0:
        raise GitError((res.stderr or res.stdout).strip())
    return res.stdout


def init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "--quiet", "--template=", "--initial-branch=main")


def commit_all(repo: Path, message: str, when: datetime, author: str) -> str:
    git(repo, "add", "--all", when=when, author=author)
    git(repo, "commit", "--quiet", "-m", message, when=when, author=author)
    return head(repo)


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def tracked_files(repo: Path) -> list[str]:
    return [x for x in git(repo, "ls-files").splitlines() if x]


def template_text(rel: str) -> str:
    return (TEMPLATE / rel).read_text()


# Earlier versions of files that changed during the project's history.
STG_PAYMENTS_V1 = """with source as (
    select *
    from {{ source('raw', 'payments') }}
    where {{ partition_predicate('_ds') }}
      and {{ not_quarantined('_ds', 'raw.payments') }}
)

select
    _ds as ds,
    payment_id,
    loan_id,
    payment_date,
    amount,
    principal_amount,
    interest_amount
from source
"""

README_V1 = """# harbor-data-pipeline

Ingestion and dbt models for the Harbor Lending warehouse (DuckDB).
"""


@dataclass(frozen=True)
class HistoryCommit:
    when: datetime
    author: str
    message: str
    files: dict[str, str | None]  # path -> template path to copy, or None to use an override


def _monitors_v1() -> str:
    text = template_text("monitoring/monitors.yml")
    start = text.index("  - name: reconciliation_payments")
    end = text.index("  - name: dbt_tests")
    return text[:start] + text[end:]


HISTORY: list[HistoryCommit] = [
    HistoryCommit(
        datetime(2026, 5, 4, 9, 12),
        "priya",
        "Initial dbt project and loan origination extract",
        {
            ".gitignore": ".gitignore",
            "README.md": None,
            "dbt/dbt_project.yml": "dbt/dbt_project.yml",
            "dbt/profiles.yml": "dbt/profiles.yml",
            "dbt/macros/partitions.sql": "dbt/macros/partitions.sql",
            "dbt/models/sources.yml": "dbt/models/sources.yml",
            "dbt/models/staging/stg_loans.sql": "dbt/models/staging/stg_loans.sql",
            "warehouse/ddl.sql": "warehouse/ddl.sql",
            "ingestion/harbor_ingest.py": "ingestion/harbor_ingest.py",
            "contracts/loans.yml": "contracts/loans.yml",
        },
    ),
    HistoryCommit(
        datetime(2026, 5, 12, 14, 40),
        "dana",
        "Add Keel Servicing payments feed",
        {
            "contracts/payments.yml": "contracts/payments.yml",
            "dbt/models/staging/stg_payments.sql": None,
        },
    ),
    HistoryCommit(
        datetime(2026, 5, 20, 10, 5),
        "dana",
        "Add weekly branch reference snapshot and dim_branch",
        {
            "contracts/branches.yml": "contracts/branches.yml",
            "dbt/models/marts/dim_branch.sql": "dbt/models/marts/dim_branch.sql",
        },
    ),
    HistoryCommit(
        datetime(2026, 6, 3, 16, 22),
        "priya",
        "Add fct_payments and daily loan balances",
        {
            "dbt/models/marts/fct_payments.sql": "dbt/models/marts/fct_payments.sql",
            "dbt/models/marts/fct_loan_balance_daily.sql": "dbt/models/marts/fct_loan_balance_daily.sql",
            "dbt/models/marts/schema.yml": "dbt/models/marts/schema.yml",
            "dbt/tests/assert_balance_daily_unique.sql": "dbt/tests/assert_balance_daily_unique.sql",
        },
    ),
    HistoryCommit(
        datetime(2026, 6, 16, 11, 30),
        "tom",
        "Delinquency mart for finance reporting",
        {
            "dbt/models/marts/mart_delinquency.sql": "dbt/models/marts/mart_delinquency.sql",
        },
    ),
    HistoryCommit(
        datetime(2026, 6, 30, 9, 48),
        "dana",
        "Meridian weekly bureau refresh",
        {
            "contracts/bureau.yml": "contracts/bureau.yml",
            "dbt/models/staging/stg_bureau.sql": "dbt/models/staging/stg_bureau.sql",
        },
    ),
    HistoryCommit(
        datetime(2026, 7, 14, 15, 3),
        "sam",
        "Monitoring: freshness, volume and schema monitors",
        {
            "monitoring/monitors.yml": None,
            "owners.yaml": "owners.yaml",
        },
    ),
    HistoryCommit(
        datetime(2026, 7, 28, 10, 17),
        "sam",
        "On-call runbooks",
        {f"runbooks/{p.name}": f"runbooks/{p.name}" for p in sorted((TEMPLATE / "runbooks").iterdir())},
    ),
    HistoryCommit(
        datetime(2026, 8, 11, 13, 55),
        "dana",
        "stg_payments: keep the latest CDC event per payment",
        {
            "dbt/models/staging/stg_payments.sql": "dbt/models/staging/stg_payments.sql",
            "dbt/models/staging/schema.yml": "dbt/models/staging/schema.yml",
        },
    ),
    HistoryCommit(
        datetime(2026, 8, 18, 17, 20),
        "priya",
        "Test for negative outstanding principal",
        {
            "dbt/tests/assert_no_negative_balances.sql": "dbt/tests/assert_no_negative_balances.sql",
        },
    ),
    HistoryCommit(
        datetime(2026, 8, 21, 9, 30),
        "sam",
        "Reconcile payments against Keel trailer totals",
        {
            "monitoring/monitors.yml": "monitoring/monitors.yml",
        },
    ),
    HistoryCommit(
        datetime(2026, 8, 26, 14, 12),
        "tom",
        "README: document the daily DAG and partition rebuilds",
        {
            "README.md": "README.md",
        },
    ),
]

OVERRIDES = {
    (1, "README.md"): README_V1,
    (2, "dbt/models/staging/stg_payments.sql"): STG_PAYMENTS_V1,
}


def build_history(repo: Path) -> list[str]:
    """Create the repository with its full history. Returns the commit SHAs in order."""
    init(repo)
    shas: list[str] = []
    for i, c in enumerate(HISTORY, start=1):
        for rel, src in c.files.items():
            dest = repo / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src is None:
                text = _monitors_v1() if rel == "monitoring/monitors.yml" else OVERRIDES[(i, rel)]
            else:
                text = template_text(src)
            dest.write_text(text)
        shas.append(commit_all(repo, c.message, c.when, c.author))
    return shas
