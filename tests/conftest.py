"""Shared fixtures. Integration tests generate a few task worlds once per session (about a
minute on first run; later runs reuse the cache under .cache/triagebench)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from triagebench.config import DEFAULT_SEED
from triagebench.episode import Episode
from triagebench.tasks import build

TEST_TASKS = ["late_file_a", "holiday_a", "cents_a", "fanout_a", "rename_a"]


@pytest.fixture(scope="session")
def generated() -> list[str]:
    build.generate(DEFAULT_SEED, TEST_TASKS, jobs=4, log=lambda s: None)
    return TEST_TASKS


@pytest.fixture
def episode(generated: list[str]) -> Iterator[Episode]:
    with Episode("late_file_a") as ep:
        yield ep
