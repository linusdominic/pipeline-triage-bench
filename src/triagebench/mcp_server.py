"""Serve one episode's tools over MCP (stdio), so any MCP-capable agent can be evaluated.

The server creates a fresh episode for the task, exposes every tool, and scores the episode
when `submit` is called or when the client disconnects. The score is written to
`--result` (JSON) for the harness that launched the server; the agent never sees it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from triagebench import __version__
from triagebench.episode import Episode


def build_server(ep: Episode, on_submit: Any) -> Server[Any]:
    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=s["name"], description=s["description"], input_schema=s["input_schema"])
                for s in ep.api.specs()
            ]
        )

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        res = ep.api.call(params.name, dict(params.arguments or {}))
        if params.name == "submit" and res.get("ok"):
            on_submit()
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(res))],
            is_error=not res.get("ok", False),
        )

    return Server(
        "triagebench",
        version=__version__,
        instructions=ep.briefing(),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def serve(task_id: str, seed: int, trace: Path | None, result: Path | None) -> None:
    ep = Episode(task_id, seed, trace)
    written = False

    def write_result() -> None:
        nonlocal written
        if result is not None and not written:
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text(json.dumps(ep.score().to_dict(), indent=2) + "\n")
            written = True

    server = build_server(ep, write_result)

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    try:
        anyio.run(main)
    finally:
        write_result()
        ep.close()
