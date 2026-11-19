"""Agent interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from triagebench.episode import Episode


@dataclass
class AgentResult:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    stop_reason: str | None = None


class Agent(Protocol):
    name: str

    def run(self, episode: Episode) -> AgentResult: ...
