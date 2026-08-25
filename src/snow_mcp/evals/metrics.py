"""Metric definitions.

Three headline metrics are reported, each defined precisely here so the numbers
in the README mean something specific.

**Tool-selection accuracy.** Per task, compare the *set* of distinct tools the
agent called against the expected set. Set-based rather than sequence-based,
because several orderings are legitimately correct; ordering is captured
separately by ``first_tool_correct``. Reported as macro-averaged precision,
recall and F1 (each task weighted equally, so easy tasks with many calls do not
dominate), plus:

* ``exact_set_match`` - the called set equals the expected set exactly
* ``forbidden_rate`` - fraction of tasks touching an explicitly wrong tool
* ``first_tool_correct`` - fraction whose first call was an acceptable opener,
  which is where a misread of the request usually shows up

**Task-completion rate.** Fraction of tasks where every graded check passes.
Checks are state assertions run against the backend after the agent finishes,
plus assertions on the final answer text. A task that produces a fluent summary
without making the required change scores zero.

**Latency per call.** MCP round-trip latency per tool call: mean, p50, p95 and
max, aggregated overall and per tool name. Model latency is tracked separately
so the transport and the model are never confused with each other.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile; stable for the small samples an eval produces."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(fraction * len(ordered) + 0.5))))
    return ordered[rank - 1]


@dataclass
class LatencyStats:
    count: int = 0
    mean_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    max_ms: float = 0.0
    total_ms: float = 0.0

    @classmethod
    def of(cls, values: Iterable[float]) -> LatencyStats:
        data = [float(value) for value in values]
        if not data:
            return cls()
        return cls(
            count=len(data),
            mean_ms=round(statistics.fmean(data), 2),
            p50_ms=round(percentile(data, 0.50), 2),
            p95_ms=round(percentile(data, 0.95), 2),
            max_ms=round(max(data), 2),
            total_ms=round(sum(data), 2),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "mean_ms": self.mean_ms,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "max_ms": self.max_ms,
            "total_ms": self.total_ms,
        }


@dataclass
class ToolSelectionScore:
    expected: list[str] = field(default_factory=list)
    called: list[str] = field(default_factory=list)
    forbidden_used: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    exact_match: bool = False
    first_tool_correct: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected": self.expected,
            "called": self.called,
            "missing": self.missing,
            "extra": self.extra,
            "forbidden_used": self.forbidden_used,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "exact_match": self.exact_match,
            "first_tool_correct": self.first_tool_correct,
        }


def score_tool_selection(
    called_sequence: Sequence[str],
    expected: Sequence[str],
    *,
    forbidden: Sequence[str] = (),
    optional: Sequence[str] = (),
    allowed_first: Sequence[str] = (),
) -> ToolSelectionScore:
    """Grade one task's tool usage.

    ``optional`` names tools that are a defensible choice for the task but not
    required: they are excluded from the precision denominator so that a
    reasonable alternative route is not scored as a mistake, while anything
    outside expected+optional still counts against precision.
    """
    called_unique: list[str] = []
    for name in called_sequence:
        if name not in called_unique:
            called_unique.append(name)

    expected_set = set(expected)
    optional_set = set(optional) - expected_set
    called_set = set(called_unique)
    hits = expected_set & called_set
    judged = called_set - optional_set

    if judged:
        precision = len(hits) / len(judged)
    else:
        precision = 1.0 if not expected_set else 0.0
    recall = len(hits) / len(expected_set) if expected_set else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    openers = set(allowed_first) or (expected_set | optional_set)
    first_correct = bool(called_sequence) and called_sequence[0] in openers
    if not expected_set and not called_sequence:
        first_correct = True

    return ToolSelectionScore(
        expected=sorted(expected_set),
        called=called_unique,
        forbidden_used=sorted(called_set & set(forbidden)),
        missing=sorted(expected_set - called_set),
        extra=sorted(judged - expected_set),
        precision=precision,
        recall=recall,
        f1=f1,
        exact_match=(judged == expected_set) or (called_set == expected_set),
        first_tool_correct=first_correct,
    )


def aggregate(results: Sequence[Any]) -> dict[str, Any]:
    """Roll per-task results up into the headline report."""
    total = len(results)
    if not total:
        return {}

    completed = [result for result in results if result.completed]
    selection = [result.selection for result in results]

    call_latencies: list[float] = []
    per_tool: dict[str, list[float]] = {}
    per_tool_errors: dict[str, int] = {}
    for result in results:
        for call in result.run.tool_calls:
            call_latencies.append(call.latency_ms)
            per_tool.setdefault(call.name, []).append(call.latency_ms)
            if not call.ok:
                per_tool_errors[call.name] = per_tool_errors.get(call.name, 0) + 1

    model_latencies = [
        turn.model_latency_ms for result in results for turn in result.run.turns
    ]
    total_calls = len(call_latencies)
    failed_calls = sum(len(result.run.failed_calls) for result in results)

    by_category: dict[str, dict[str, Any]] = {}
    for result in results:
        bucket = by_category.setdefault(
            result.task.category, {"tasks": 0, "completed": 0, "f1_sum": 0.0}
        )
        bucket["tasks"] += 1
        bucket["completed"] += int(result.completed)
        bucket["f1_sum"] += result.selection.f1
    for bucket in by_category.values():
        bucket["completion_rate"] = round(bucket["completed"] / bucket["tasks"], 4)
        bucket["mean_f1"] = round(bucket["f1_sum"] / bucket["tasks"], 4)
        bucket.pop("f1_sum")

    return {
        "tasks": total,
        "task_completion": {
            "completed": len(completed),
            "rate": round(len(completed) / total, 4),
            "failed_task_ids": [result.task.id for result in results if not result.completed],
        },
        "tool_selection": {
            "macro_precision": round(
                sum(score.precision for score in selection) / total, 4
            ),
            "macro_recall": round(sum(score.recall for score in selection) / total, 4),
            "macro_f1": round(sum(score.f1 for score in selection) / total, 4),
            "exact_set_match_rate": round(
                sum(score.exact_match for score in selection) / total, 4
            ),
            "first_tool_accuracy": round(
                sum(score.first_tool_correct for score in selection) / total, 4
            ),
            "forbidden_rate": round(
                sum(bool(score.forbidden_used) for score in selection) / total, 4
            ),
        },
        "latency": {
            "per_tool_call_ms": LatencyStats.of(call_latencies).to_dict(),
            "per_model_turn_ms": LatencyStats.of(model_latencies).to_dict(),
            "per_task_wall_ms": LatencyStats.of(
                [result.run.wall_ms for result in results]
            ).to_dict(),
            "by_tool": {
                name: LatencyStats.of(values).to_dict()
                for name, values in sorted(per_tool.items())
            },
        },
        "reliability": {
            "total_tool_calls": total_calls,
            "failed_tool_calls": failed_calls,
            "tool_error_rate": round(failed_calls / total_calls, 4) if total_calls else 0.0,
            "errors_by_tool": per_tool_errors,
            "mean_calls_per_task": round(total_calls / total, 2),
            "runs_hitting_turn_limit": sum(
                1 for result in results if result.run.stop_reason == "max_turns"
            ),
        },
        "cost": {
            "input_tokens": sum(result.run.input_tokens for result in results),
            "output_tokens": sum(result.run.output_tokens for result in results),
            "mean_input_tokens_per_task": round(
                sum(result.run.input_tokens for result in results) / total, 1
            ),
        },
        "by_category": by_category,
    }
