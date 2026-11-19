"""Built-in agents on a few generated tasks, and the LLM loop with a fake client."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from triagebench import runner
from triagebench.agents.llm import PRICES, LLMAgent
from triagebench.episode import Episode


def test_oracle_resolves(generated: list[str]) -> None:
    rows = runner.run_agents(["oracle"], generated, jobs=1, log=lambda s: None)["oracle"]
    assert [r["task"] for r in rows if not r["resolved"]] == []


def test_baselines_fail_in_telling_ways(generated: list[str]) -> None:
    res = runner.run_agents(
        ["always_close", "always_escalate", "fix_everything"], generated, jobs=1, log=lambda s: None
    )
    by = {a: {r["task"]: r for r in rows} for a, rows in res.items()}
    assert by["always_close"]["holiday_a"]["resolved"]
    assert not by["always_close"]["late_file_a"]["resolved"]
    assert by["always_escalate"]["rename_a"]["resolved"]
    assert not by["always_escalate"]["holiday_a"]["resolved"]
    assert by["always_escalate"]["holiday_a"]["unnecessary_actions"] == 1
    # Dividing the cents out of the raw table by hand is the forbidden kind of fix.
    assert by["fix_everything"]["cents_a"]["forbidden_actions"] >= 1
    # Patching the mapping for an unannounced rename loads data the vendor must re-send.
    assert not by["fix_everything"]["rename_a"]["resolved"]


class FakeMessages:
    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.script.pop(0)


def _resp(blocks: list[Any], stop: str) -> Any:
    usage = SimpleNamespace(
        input_tokens=1000, output_tokens=200, cache_read_input_tokens=3000, cache_creation_input_tokens=500
    )
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=usage, model="claude-opus-5-5")


def _use(i: str, name: str, inp: dict[str, Any]) -> Any:
    return SimpleNamespace(type="tool_use", id=i, name=name, input=inp)


def test_llm_agent_with_fake_client(generated: list[str]) -> None:
    script = [
        _resp(
            [
                SimpleNamespace(type="thinking", thinking="", signature="sig"),
                _use("t1", "list_alerts", {}),
                _use("t2", "get_calendar", {}),
            ],
            "tool_use",
        ),
        _resp(
            [
                _use(
                    "t3",
                    "submit",
                    {
                        "decision": "no_action",
                        "root_cause": "source:calendar",
                        "evidence": ["holiday"],
                        "summary": "Bank holiday.",
                    },
                )
            ],
            "tool_use",
        ),
    ]
    messages = FakeMessages(script)
    client = SimpleNamespace(messages=messages)
    agent = LLMAgent(client, "claude-opus-5-5")
    with Episode("holiday_a") as ep:
        res = agent.run(ep)
        score = ep.score(res.input_tokens, res.output_tokens)
    assert res.stop_reason == "submitted"
    assert score.resolved and score.tool_calls == 3
    first = messages.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "high"}
    assert "temperature" not in first and "tool_choice" not in first
    assert {t["name"] for t in first["tools"]} >= {"submit", "list_alerts", "query_warehouse"}
    second = messages.calls[1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"][0].type == "thinking"
    results = second[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert res.input_tokens == 2 * (1000 + 3000 + 500) and res.output_tokens == 400
    p_in, p_out, p_read = PRICES["claude-opus-5-5"]
    expected = (2000 * p_in + 1000 * p_in * 1.25 + 6000 * p_read + 400 * p_out) / 1e6
    assert abs((res.cost_usd or 0) - expected) < 1e-9


def test_llm_agent_nudges_once_then_stops(generated: list[str]) -> None:
    text = SimpleNamespace(type="text", text="I think it is fine.")
    messages = FakeMessages([_resp([text], "end_turn"), _resp([text], "end_turn")])
    agent = LLMAgent(SimpleNamespace(messages=messages), "claude-sonnet-5-5")
    with Episode("holiday_a") as ep:
        res = agent.run(ep)
        score = ep.score()
    assert res.stop_reason == "end_turn" and len(messages.calls) == 2
    assert not score.submitted and not score.resolved
    assert res.cost_usd is None  # the fake reports a different model than requested
