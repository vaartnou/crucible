"""A small OpenAI-compatible tool loop; no provider SDK or hidden task access."""

import json
from collections import deque
from typing import Any

import httpx

from crucible.core.task import TaskSpec
from crucible.core.tools import Observation, ToolCall, ToolSpec


class CompatiblePolicy:
    """Preserves assistant tool-call IDs and returns each result to its matching call."""

    def __init__(self, client: httpx.AsyncClient, model: str) -> None:
        self.client = client
        self.model = model
        self.name = model
        self.messages: list[dict[str, Any]] = []
        self.schemas: list[dict[str, Any]] = []
        self.pending: deque[tuple[str, ToolCall]] = deque()
        self.last_id: str | None = None

    async def reset(self, task_spec: TaskSpec, tools: list[ToolSpec], instruction: str) -> None:
        self.messages = [{"role": "system", "content": instruction}]
        self.schemas = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.json_schema(),
                },
            }
            for tool in tools
        ]
        self.pending.clear()
        self.last_id = None

    async def act(self, obs: Observation) -> ToolCall | None:
        if self.last_id is not None:
            self.messages.append(
                {"role": "tool", "tool_call_id": self.last_id, "content": obs.text}
            )
            self.last_id = None
        elif not self.messages or len(self.messages) == 1:
            self.messages.append({"role": "user", "content": obs.text or "Begin the incident."})
        if not self.pending:
            response = await self.client.post(
                "chat/completions",
                json={
                    "model": self.model,
                    "messages": self.messages,
                    "tools": self.schemas,
                    "tool_choice": "auto",
                },
            )
            response.raise_for_status()
            message: dict[str, Any] = response.json()["choices"][0]["message"]
            self.messages.append(message)
            for raw in message.get("tool_calls", []):
                function = raw["function"]
                self.pending.append(
                    (
                        raw["id"],
                        ToolCall(name=function["name"], args=json.loads(function["arguments"])),
                    )
                )
        if not self.pending:
            return None
        self.last_id, call = self.pending.popleft()
        return call
