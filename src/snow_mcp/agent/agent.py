"""The agent loop: Claude on one side, an MCP server on the other.

The loop itself is deliberately small. Everything interesting is in the
instrumentation, because the deliverable of this project is not "an agent that
works" but "an agent whose behaviour is measured".

Every run produces an :class:`AgentRun` containing the ordered tool calls with
per-call latency, per-turn model latency and token usage, the stop reason, and
the final answer — which is exactly the input the eval harness grades.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .bridge import MCPToolBridge, ToolCallRecord
from .llm import LLMClient, LLMResponse
from .prompts import get_prompt

MAX_TOOL_RESULT_CHARS = 8000


@dataclass
class TurnRecord:
    index: int
    model_latency_ms: float
    input_tokens: int
    output_tokens: int
    stop_reason: str
    tool_calls: list[str] = field(default_factory=list)
    text: str = ""


@dataclass
class AgentRun:
    """Everything observable about one task execution."""

    task: str
    answer: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    stop_reason: str = ""
    error: str | None = None
    wall_ms: float = 0.0
    model: str = ""
    prompt_variant: str = ""

    @property
    def tool_sequence(self) -> list[str]:
        return [call.name for call in self.tool_calls]

    @property
    def unique_tools(self) -> list[str]:
        seen: list[str] = []
        for name in self.tool_sequence:
            if name not in seen:
                seen.append(name)
        return seen

    @property
    def failed_calls(self) -> list[ToolCallRecord]:
        return [call for call in self.tool_calls if not call.ok]

    @property
    def total_tool_ms(self) -> float:
        return sum(call.latency_ms for call in self.tool_calls)

    @property
    def total_model_ms(self) -> float:
        return sum(turn.model_latency_ms for turn in self.turns)

    @property
    def input_tokens(self) -> int:
        return sum(turn.input_tokens for turn in self.turns)

    @property
    def output_tokens(self) -> int:
        return sum(turn.output_tokens for turn in self.turns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "answer": self.answer,
            "model": self.model,
            "prompt_variant": self.prompt_variant,
            "stop_reason": self.stop_reason,
            "error": self.error,
            "wall_ms": round(self.wall_ms, 2),
            "total_tool_ms": round(self.total_tool_ms, 2),
            "total_model_ms": round(self.total_model_ms, 2),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_sequence": self.tool_sequence,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "turns": [asdict(turn) for turn in self.turns],
        }


class ITSMAgent:
    """Drives an LLM through an MCP tool loop until it produces a final answer."""

    def __init__(
        self,
        llm: LLMClient,
        bridge: MCPToolBridge,
        *,
        prompt_variant: str = "operator",
        max_turns: int = 12,
        max_tokens: int = 2048,
    ):
        self.llm = llm
        self.bridge = bridge
        self.prompt_variant = prompt_variant
        self.system = get_prompt(prompt_variant)
        self.max_turns = max_turns
        self.max_tokens = max_tokens

    async def run(self, task: str, *, context: str | None = None) -> AgentRun:
        run = AgentRun(
            task=task,
            model=getattr(self.llm, "model", "unknown"),
            prompt_variant=self.prompt_variant,
        )
        self.bridge.reset_calls()
        started = time.perf_counter()

        user_content = task if not context else f"{context}\n\n{task}"
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        tools = self.bridge.tool_specs

        try:
            for index in range(self.max_turns):
                response = await self.llm.complete(
                    system=self.system,
                    messages=messages,
                    tools=tools,
                    max_tokens=self.max_tokens,
                )
                turn = TurnRecord(
                    index=index,
                    model_latency_ms=response.latency_ms,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    stop_reason=response.stop_reason,
                    text=response.text,
                )
                run.turns.append(turn)
                messages.append({"role": "assistant", "content": self._assistant_content(response)})

                tool_uses = response.tool_uses
                if not tool_uses:
                    run.answer = response.text
                    run.stop_reason = response.stop_reason
                    break

                results = []
                for block in tool_uses:
                    name = block.get("name", "")
                    arguments = block.get("input", {}) or {}
                    turn.tool_calls.append(name)
                    record = await self.bridge.call(name, arguments)
                    run.tool_calls.append(record)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("id", ""),
                        "content": record.result_text[:MAX_TOOL_RESULT_CHARS]
                        or (record.error or "(empty result)"),
                        "is_error": not record.ok,
                    })
                messages.append({"role": "user", "content": results})
            else:
                run.stop_reason = "max_turns"
                run.answer = run.turns[-1].text if run.turns else ""
                run.error = f"stopped after {self.max_turns} turns without a final answer"
        except Exception as exc:  # network failure, auth, malformed schema
            run.error = f"{type(exc).__name__}: {exc}"
            run.stop_reason = "error"

        run.wall_ms = (time.perf_counter() - started) * 1000.0
        return run

    @staticmethod
    def _assistant_content(response: LLMResponse) -> list[dict[str, Any]]:
        """Strip provider-specific keys the API will not accept back as input."""
        cleaned: list[dict[str, Any]] = []
        for block in response.content:
            kind = block.get("type")
            if kind == "text":
                if block.get("text"):
                    cleaned.append({"type": "text", "text": block["text"]})
            elif kind == "tool_use":
                cleaned.append({
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input", {}),
                })
            elif kind == "thinking":
                cleaned.append(block)
        return cleaned or [{"type": "text", "text": "(no content)"}]


def parse_tool_json(record: ToolCallRecord) -> Any:
    """Decode a tool result, returning ``None`` when it is not JSON."""
    try:
        return json.loads(record.result_text)
    except (json.JSONDecodeError, TypeError):
        return None
