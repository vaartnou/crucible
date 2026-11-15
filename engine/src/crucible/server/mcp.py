"""The MCP server for one episode: the task's tools, called through an `EpisodeSession`."""

import asyncio
import json
from typing import TYPE_CHECKING, Any

import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from pydantic import JsonValue

from crucible.core.session import EpisodeSession
from crucible.core.tools import ToolCall

if TYPE_CHECKING:
    from crucible.server.spectator import Spectator


def build_mcp_server(
    session: EpisodeSession, lock: asyncio.Lock, spectator: Spectator | None = None
) -> Server[dict[str, Any]]:
    """Build the low-level MCP server. Tools come from the task's `ToolSpec`s at runtime.

    `lock` allows one call at a time, as in the HTTP API. A bad call never raises: the
    `ToolResult` says what was wrong and `isError` is set.
    """
    specs = session.task.tools()

    async def list_tools(
        ctx: ServerRequestContext[dict[str, Any]], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=s.name, description=s.description, input_schema=s.json_schema())
                for s in specs
            ]
        )

    async def call_tool(
        ctx: ServerRequestContext[dict[str, Any]], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        args: dict[str, JsonValue] = dict(params.arguments or {})
        async with lock:
            call = ToolCall(name=params.name, args=args)
            before = spectator.before_step(call) if spectator else None
            result = await session.step(call)
            if spectator:
                try:
                    spectator.after_step(call, result, before)
                except OSError:
                    # The decision already happened. Stop rather than allow an unseen
                    # continuation after a failed live write.
                    await session.close(error="spectator feed write failed")
                    raise
                if (
                    session.closed
                    or (not result.ok and session.steps_used >= session.max_steps)
                    or (result.ok and session.task.done())
                ):
                    reason, score = await session.close()
                    spectator.finalize(reason, score)
        dumped = result.model_dump(mode="json")
        return types.CallToolResult(
            content=[types.TextContent(text=json.dumps(dumped, sort_keys=True))],
            structured_content=dumped,
            is_error=not result.ok,
        )

    return Server(
        "crucible",
        instructions=session.task.instruction(),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


__all__ = ["build_mcp_server"]
