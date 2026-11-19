"""The MCP server over stdio, driven by the MCP Python client."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp_types import TextContent


def test_mcp_server_round_trip(generated: list[str], tmp_path: Path) -> None:
    result = tmp_path / "score.json"
    trace = tmp_path / "trace.jsonl"
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "triagebench.cli",
            "serve-mcp",
            "--task",
            "holiday_a",
            "--result",
            str(result),
            "--trace",
            str(trace),
        ],
        cwd=str(Path.cwd()),
    )

    async def main() -> list[str]:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            res = await session.call_tool("list_alerts", {})
            block = res.content[0]
            assert isinstance(block, TextContent)
            alerts = json.loads(block.text)["result"]
            assert {a["monitor"] for a in alerts} == {"freshness_raw_payments", "freshness_mart_delinquency"}
            bad = await session.call_tool("query_warehouse", {"sql": "drop table raw.loans"})
            assert bad.is_error
            await session.call_tool(
                "submit",
                {
                    "decision": "no_action",
                    "root_cause": "source:calendar",
                    "evidence": [a["id"] for a in alerts],
                    "summary": "holiday",
                },
            )
            return names

    names = anyio.run(main)
    assert "submit" in names and "query_warehouse" in names and len(names) == 32
    score = json.loads(result.read_text())
    assert score["resolved"] and score["tool_calls"] == 3
    assert len(trace.read_text().splitlines()) == 3
