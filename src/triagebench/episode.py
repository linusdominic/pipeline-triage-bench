"""An episode: a fresh copy of a task's world, the tool API on it, and scoring at the end."""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from types import TracebackType
from typing import Any

from triagebench.config import DEFAULT_SEED, episodes_dir
from triagebench.scoring import TaskScore, score_episode
from triagebench.tasks import build
from triagebench.tools import ToolAPI
from triagebench.world.world import World

BRIEFING = """You are the on-call engineer for the data platform at Harbor Lending, a fictional lender. \
It is {now}. Monitors have raised the alerts listed by list_alerts.

Find out what happened and do what the situation requires: that can be nothing at all. Write tools \
change the warehouse, the pipeline repository, the monitors or send messages; use them only when \
they are needed, and prefer the smallest correct action. You cannot ask anyone questions during the \
episode.

Finish by calling submit exactly once with your decision, the root-cause component, your evidence \
and a short summary."""


class Episode:
    def __init__(self, task_id: str, seed: int = DEFAULT_SEED, trace_path: Path | None = None) -> None:
        if not build.is_built(seed, task_id):
            raise RuntimeError(f"task {task_id} is not generated for seed {seed}; run `triagebench generate`")
        self.task_id = task_id
        self.seed = seed
        self.dir = episodes_dir() / f"ep-{secrets.token_hex(6)}"
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        self.world = World(build.task_dir(seed, task_id) / "world").copy_to(self.dir)
        self.api = ToolAPI(self.world, trace_path)
        self.closed = False

    def briefing(self) -> str:
        return BRIEFING.format(now=self.world.state()["now"])

    def ground_truth(self) -> dict[str, Any]:
        """Privileged: only the scorer and the oracle agent read this."""
        return build.load_ground_truth(self.seed, self.task_id)

    def score(self, input_tokens: int | None = None, output_tokens: int | None = None) -> TaskScore:
        return score_episode(
            self.ground_truth(), self.world, self.api.calls, self.api.submission, input_tokens, output_tokens
        )

    def close(self) -> None:
        if not self.closed:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.closed = True

    def __enter__(self) -> Episode:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
