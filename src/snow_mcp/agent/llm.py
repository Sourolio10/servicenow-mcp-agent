"""LLM abstraction.

The agent loop depends on this narrow interface rather than on the Anthropic
SDK directly, for two reasons:

* the test suite and CI must run with no API key and no network
* swapping models (or replaying a recorded trace) must not touch the loop

:class:`ScriptedLLM` is a deterministic stand-in used by the unit tests. It is
never used by the eval runner unless explicitly selected, and the eval report
records which provider produced the numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = os.environ.get("SNOW_AGENT_MODEL", "claude-sonnet-5")


@dataclass
class LLMResponse:
    """Provider-agnostic view of one assistant turn."""

    content: list[dict[str, Any]]
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    model: str = ""

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [block for block in self.content if block.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(
            block.get("text", "") for block in self.content if block.get("type") == "text"
        ).strip()


class LLMClient(Protocol):
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse: ...


class AnthropicLLM:
    """Anthropic Messages API implementation."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        *,
        effort: str | None = None,
        max_retries: int = 3,
        timeout: float = 120.0,
    ):
        from anthropic import AsyncAnthropic

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it, or run with --llm scripted "
                "for an offline smoke test."
            )
        self.model = model
        self.effort = effort
        self._client = AsyncAnthropic(api_key=key, max_retries=max_retries, timeout=timeout)

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
    ) -> LLMResponse:
        import time

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}

        started = time.perf_counter()
        message = await self._client.messages.create(**kwargs)
        elapsed = (time.perf_counter() - started) * 1000.0

        content = [block.model_dump() for block in message.content]
        usage = getattr(message, "usage", None)
        return LLMResponse(
            content=content,
            stop_reason=message.stop_reason or "end_turn",
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=elapsed,
            model=self.model,
        )


@dataclass
class ScriptedTurn:
    """One canned assistant turn: either tool calls or a final text answer."""

    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    text: str = ""

    def to_response(self, counter: int) -> LLMResponse:
        blocks: list[dict[str, Any]] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for index, (name, arguments) in enumerate(self.tool_calls):
            blocks.append({
                "type": "tool_use",
                "id": f"toolu_scripted_{counter}_{index}",
                "name": name,
                "input": arguments,
            })
        return LLMResponse(
            content=blocks,
            stop_reason="tool_use" if self.tool_calls else "end_turn",
            input_tokens=0,
            output_tokens=0,
            model="scripted",
        )


class ScriptedLLM:
    """Deterministic offline client that replays a fixed list of turns."""

    model = "scripted"

    def __init__(self, turns: list[ScriptedTurn]):
        self.turns = list(turns)
        self.calls = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
    ) -> LLMResponse:
        self.seen_messages.append(list(messages))
        if self.calls >= len(self.turns):
            return LLMResponse(
                content=[{"type": "text", "text": "No further scripted turns."}],
                stop_reason="end_turn",
                model="scripted",
            )
        turn = self.turns[self.calls]
        self.calls += 1
        return turn.to_response(self.calls)


def build_llm(provider: str = "anthropic", model: str = DEFAULT_MODEL, **kwargs: Any) -> LLMClient:
    if provider == "anthropic":
        return AnthropicLLM(model=model, **kwargs)
    if provider == "scripted":
        return ScriptedLLM(kwargs.get("turns", []))
    raise ValueError(f"Unknown LLM provider {provider!r}")
