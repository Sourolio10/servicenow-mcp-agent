"""Eval runner.

For each task: reset the fixture, build a fresh MCP session, run the agent,
grade the result through the same session, and record everything.

Isolation matters more than speed here — one task resolving an incident must
not change the answer to another — so state is reset per task rather than per
suite. Tasks can be run concurrently with ``--concurrency`` since each gets its
own backend instance.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..agent.agent import AgentRun, ITSMAgent
from ..agent.bridge import MCPToolBridge
from ..agent.llm import LLMClient
from ..backends.mock import MockBackend
from ..config import Settings
from ..server import build_server
from ..store import ITSMStore
from .checks import CheckResult, Grader
from .metrics import ToolSelectionScore, aggregate, score_tool_selection

TASKS_PATH = Path(__file__).parent / "tasks.yaml"


@dataclass
class Task:
    id: str
    prompt: str
    category: str = "general"
    difficulty: str = "medium"
    expected_tools: list[str] = field(default_factory=list)
    optional_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    allowed_first: list[str] = field(default_factory=list)
    state_checks: list[dict[str, Any]] = field(default_factory=list)
    answer_checks: list[dict[str, Any]] = field(default_factory=list)
    context: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Task:
        return cls(
            id=raw["id"],
            prompt=raw["prompt"].strip(),
            category=raw.get("category", "general"),
            difficulty=raw.get("difficulty", "medium"),
            expected_tools=list(raw.get("expected_tools", [])),
            optional_tools=list(raw.get("optional_tools", [])),
            forbidden_tools=list(raw.get("forbidden_tools", [])),
            allowed_first=list(raw.get("allowed_first", [])),
            state_checks=list(raw.get("state_checks", [])),
            answer_checks=list(raw.get("answer_checks", [])),
            context=raw.get("context"),
        )


@dataclass
class TaskResult:
    task: Task
    run: AgentRun
    selection: ToolSelectionScore
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        """A task completes only if every check passes and no forbidden tool was used."""
        if self.run.error and self.run.stop_reason == "error":
            return False
        if self.selection.forbidden_used:
            return False
        if not self.checks:
            # No checks declared means the task is graded purely on not failing.
            return not self.run.error
        return all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task.id,
            "category": self.task.category,
            "difficulty": self.task.difficulty,
            "prompt": self.task.prompt,
            "completed": self.completed,
            "tool_selection": self.selection.to_dict(),
            "checks": [check.to_dict() for check in self.checks],
            "failed_checks": [check.to_dict() for check in self.failed_checks],
            "run": self.run.to_dict(),
        }


def load_tasks(path: Path | str = TASKS_PATH, *, only: Sequence[str] = (), categories: Sequence[str] = ()) -> list[Task]:
    raw = yaml.safe_load(Path(path).read_text())
    tasks = [Task.from_dict(item) for item in raw.get("tasks", [])]
    if only:
        wanted = set(only)
        tasks = [task for task in tasks if task.id in wanted]
    if categories:
        wanted = set(categories)
        tasks = [task for task in tasks if task.category in wanted]
    return tasks


class EvalRunner:
    """Executes a suite of tasks against a freshly built server per task."""

    def __init__(
        self,
        llm_factory: Any,
        *,
        prompt_variant: str = "operator",
        max_turns: int = 12,
        settings: Settings | None = None,
        trace_path: Path | str | None = None,
    ):
        self.llm_factory = llm_factory
        self.prompt_variant = prompt_variant
        self.max_turns = max_turns
        self.settings = settings or Settings.from_env()
        self.trace_path = Path(trace_path) if trace_path else None

    def _fresh_server(self) -> Any:
        """A server with its own store, so tasks never share state."""
        backend = MockBackend(store=ITSMStore(), read_only=self.settings.read_only)
        return build_server(backend=backend, settings=self.settings)

    async def run_task(self, task: Task, llm: LLMClient) -> TaskResult:
        server = self._fresh_server()
        async with MCPToolBridge(target=server) as bridge:
            agent = ITSMAgent(
                llm,
                bridge,
                prompt_variant=self.prompt_variant,
                max_turns=self.max_turns,
            )
            run = await agent.run(task.prompt, context=task.context)
            selection = score_tool_selection(
                run.tool_sequence,
                task.expected_tools,
                forbidden=task.forbidden_tools,
                optional=task.optional_tools,
                allowed_first=task.allowed_first,
            )
            grader = Grader(bridge)
            checks = await grader.grade(task, run.answer)
        return TaskResult(task=task, run=run, selection=selection, checks=checks)

    async def run_suite(
        self,
        tasks: Sequence[Task],
        *,
        concurrency: int = 1,
        on_result: Any = None,
    ) -> list[TaskResult]:
        semaphore = asyncio.Semaphore(max(1, concurrency))
        results: list[TaskResult | None] = [None] * len(tasks)

        async def worker(index: int, task: Task) -> None:
            async with semaphore:
                llm = self.llm_factory()
                try:
                    result = await self.run_task(task, llm)
                except Exception as exc:  # a harness failure must not lose the suite
                    run = AgentRun(task=task.prompt, error=f"{type(exc).__name__}: {exc}", stop_reason="error")
                    result = TaskResult(
                        task=task,
                        run=run,
                        selection=score_tool_selection([], task.expected_tools,
                                                       forbidden=task.forbidden_tools,
                                                       optional=task.optional_tools),
                    )
                results[index] = result
                if on_result:
                    on_result(result)

        await asyncio.gather(*(worker(index, task) for index, task in enumerate(tasks)))
        final = [result for result in results if result is not None]
        if self.trace_path:
            self.write_traces(final)
        return final

    def write_traces(self, results: Sequence[TaskResult]) -> None:
        assert self.trace_path is not None
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trace_path.open("w", encoding="utf-8") as handle:
            for result in results:
                handle.write(json.dumps(result.to_dict(), default=str) + "\n")


def summarise(results: Sequence[TaskResult], *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Produce the full report document."""
    report = {
        "metadata": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **(metadata or {}),
        },
        "summary": aggregate(results),
        "tasks": [
            {
                "id": result.task.id,
                "category": result.task.category,
                "difficulty": result.task.difficulty,
                "completed": result.completed,
                "tool_f1": round(result.selection.f1, 3),
                "tools_called": result.selection.called,
                "tools_missing": result.selection.missing,
                "tools_extra": result.selection.extra,
                "forbidden_used": result.selection.forbidden_used,
                "calls": len(result.run.tool_calls),
                "tool_ms": round(result.run.total_tool_ms, 1),
                "model_ms": round(result.run.total_model_ms, 1),
                "wall_ms": round(result.run.wall_ms, 1),
                "failed_checks": [check.description for check in result.failed_checks],
                "error": result.run.error,
            }
            for result in results
        ],
    }
    return report
