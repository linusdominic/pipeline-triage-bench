"""An agent that drives a Claude model through the episode's tools (Anthropic Messages API).

A manual tool-use loop: send the briefing and the tool definitions, execute every tool_use
block the model returns, send all results back in one user message, and stop when the model
calls `submit`, stops calling tools, or hits the turn limit. The assistant content is
appended unchanged (thinking blocks included), as the API requires.

Needs the `llm` extra (`uv sync --extra llm`) and credentials for the Anthropic SDK
(ANTHROPIC_API_KEY). It is never run by the tests; they use a fake client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from triagebench.agents.base import AgentResult
from triagebench.episode import Episode

DEFAULT_MODEL = "claude-opus-5-5"

# US dollars per million tokens: (input, output, cache read). Cache writes (5-minute TTL)
# cost 1.25 times the input price. Prices as published for these models in November 2026.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
}
SUPPORTED_MODELS = tuple(PRICES)

SYSTEM = """You are an autonomous on-call agent for a data platform. You act only through the tools \
provided. Investigate before acting. Each write action changes real state; take only the actions \
the situation needs. When you are done, call the submit tool exactly once."""

NUDGE = "Call the submit tool to finish the episode with your decision."


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    requests: int = 0
    models: list[str] = field(default_factory=list)

    def add(self, usage: Any, model: str | None) -> None:
        self.requests += 1
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_read_input_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        self.cache_creation_input_tokens += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        if model and model not in self.models:
            self.models.append(model)

    @property
    def prompt_tokens(self) -> int:
        """All input tokens: uncached, cache writes and cache reads."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

    def cost(self, model: str) -> float | None:
        if model not in PRICES or any(m != model for m in self.models):
            return None  # unknown price, or a different model served part of the episode
        p_in, p_out, p_read = PRICES[model]
        return round(
            (
                self.input_tokens * p_in
                + self.cache_creation_input_tokens * p_in * 1.25
                + self.cache_read_input_tokens * p_read
                + self.output_tokens * p_out
            )
            / 1_000_000,
            6,
        )


def _dump(block: Any) -> Any:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    if isinstance(block, dict):
        return block
    return {k: v for k, v in vars(block).items() if not k.startswith("_")}


class LLMAgent:
    def __init__(
        self,
        client: Any,
        model: str = DEFAULT_MODEL,
        max_turns: int = 40,
        effort: str = "high",
        max_tokens: int = 16000,
        fallbacks: bool = False,
    ) -> None:
        self.client = client
        self.model = model
        self.name = f"llm:{model}"
        self.max_turns = max_turns
        self.effort = effort
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        self.transcript: list[dict[str, Any]] = []
        self.usage = Usage()

    def _create(self, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM,
            "tools": tools,
            "messages": messages,
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self.fallbacks:
            # Off by default: a benchmark run should measure the model it names. With this on, a
            # declined request is re-run on Anthropic's recommended fallback model.
            return self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        return self.client.messages.create(**kwargs)

    def run(self, episode: Episode) -> AgentResult:
        tools = [
            {"name": s["name"], "description": s["description"], "input_schema": s["input_schema"]}
            for s in episode.api.specs()
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": episode.briefing()}]
        self.transcript = [{"role": "user", "content": episode.briefing()}]
        self.usage = Usage()
        stop = "max_turns"
        nudged = False
        for _ in range(self.max_turns):
            try:
                resp = self._create(tools, messages)
            except Exception as exc:  # API errors end the episode; the score reflects what was done
                stop = f"api_error: {type(exc).__name__}: {exc}"
                break
            self.usage.add(getattr(resp, "usage", None), getattr(resp, "model", None))
            content = list(getattr(resp, "content", []) or [])
            self.transcript.append(
                {"role": "assistant", "stop_reason": resp.stop_reason, "content": [_dump(b) for b in content]}
            )
            if resp.stop_reason == "refusal":
                stop = "refusal"
                break
            messages.append({"role": "assistant", "content": content})
            if resp.stop_reason == "pause_turn":
                continue
            if resp.stop_reason == "max_tokens":
                stop = "max_tokens"
                break
            uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
            if not uses:
                if episode.api.submission is not None or nudged:
                    stop = "end_turn"
                    break
                nudged = True
                messages.append({"role": "user", "content": NUDGE})
                self.transcript.append({"role": "user", "content": NUDGE})
                continue
            results = []
            for b in uses:
                out = episode.api.call(b.name, dict(b.input or {}))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": json.dumps(out),
                        "is_error": not out.get("ok", False),
                    }
                )
            messages.append({"role": "user", "content": results})
            self.transcript.append({"role": "user", "content": results})
            if episode.api.submission is not None:
                stop = "submitted"
                break
        return AgentResult(
            input_tokens=self.usage.prompt_tokens,
            output_tokens=self.usage.output_tokens,
            cost_usd=self.usage.cost(self.model),
            stop_reason=stop,
        )


def make_client() -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise SystemExit("the LLM runner needs the llm extra: uv sync --extra llm") from exc
    return anthropic.Anthropic()
