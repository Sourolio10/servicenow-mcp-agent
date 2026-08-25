"""Grading checks.

A task is graded on two things:

* **state checks** — after the agent finishes, the grader calls read-only tools
  through the same MCP session and asserts on the JSON that comes back. Grading
  through the protocol rather than by reaching into the store means the eval
  also proves the change is visible over MCP, and works unchanged against a
  live ServiceNow instance.
* **answer checks** — substring and regex assertions on the final message, used
  for questions whose deliverable is information rather than a record change.

Grader tool calls are made after the run and are excluded from the tool-selection
and latency metrics.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..agent.bridge import MCPToolBridge


@dataclass
class CheckResult:
    description: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.description, "passed": self.passed, "detail": self.detail}


def dig(payload: Any, path: str) -> Any:
    """Resolve a dotted path with list indexing, e.g. ``incidents.0.number``.

    ``*`` collects across a list: ``incidents.*.number`` yields a list.
    """
    current = payload
    for part in path.split("."):
        if current is None:
            return None
        if part == "*":
            if not isinstance(current, list):
                return None
            return current
        if isinstance(current, list):
            if part.isdigit():
                index = int(part)
                current = current[index] if index < len(current) else None
            else:
                collected = [
                    item.get(part) if isinstance(item, dict) else None for item in current
                ]
                current = collected
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _flatten(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten(item))
        return out
    return [str(value)]


def compare(actual: Any, expectation: dict[str, Any]) -> tuple[bool, str]:
    """Apply one assertion operator to a resolved value."""
    values = _flatten(actual)
    joined = " ".join(values).lower()

    if "equals" in expectation:
        want = str(expectation["equals"])
        return (len(values) == 1 and values[0] == want), f"got {actual!r}, want {want!r}"
    if "equals_any" in expectation:
        wanted = {str(item) for item in expectation["equals_any"]}
        return (bool(values) and values[0] in wanted), f"got {actual!r}, want one of {sorted(wanted)}"
    if "contains" in expectation:
        want = str(expectation["contains"]).lower()
        return (want in joined), f"{want!r} not found in {joined[:200]!r}"
    if "contains_all" in expectation:
        missing = [str(item) for item in expectation["contains_all"] if str(item).lower() not in joined]
        return (not missing), f"missing {missing}"
    if "contains_any" in expectation:
        hit = any(str(item).lower() in joined for item in expectation["contains_any"])
        return hit, f"none of {expectation['contains_any']} found"
    if "not_contains" in expectation:
        want = str(expectation["not_contains"]).lower()
        return (want not in joined), f"{want!r} unexpectedly present"
    if "matches" in expectation:
        pattern = re.compile(str(expectation["matches"]), re.IGNORECASE)
        return bool(pattern.search(joined)), f"pattern {expectation['matches']!r} did not match"
    if "not_matches" in expectation:
        pattern = re.compile(str(expectation["not_matches"]), re.IGNORECASE)
        found = pattern.search(joined)
        return (not found), f"pattern matched unexpectedly: {found.group(0) if found else ''!r}"
    if "min_length" in expectation:
        return (len(values) >= int(expectation["min_length"])), f"got {len(values)} items"
    if "length" in expectation:
        return (len(values) == int(expectation["length"])), f"got {len(values)} items"
    if "gte" in expectation:
        try:
            return (float(values[0]) >= float(expectation["gte"])), f"got {actual!r}"
        except (ValueError, IndexError):
            return False, f"non-numeric value {actual!r}"
    if "lte" in expectation:
        try:
            return (float(values[0]) <= float(expectation["lte"])), f"got {actual!r}"
        except (ValueError, IndexError):
            return False, f"non-numeric value {actual!r}"
    return False, f"unknown expectation {expectation!r}"


@dataclass
class Grader:
    """Runs a task's checks against the live MCP session and the final answer."""

    bridge: MCPToolBridge
    results: list[CheckResult] = field(default_factory=list)

    async def grade(self, task: Any, answer: str) -> list[CheckResult]:
        results: list[CheckResult] = []

        for spec in task.state_checks:
            tool = spec["tool"]
            arguments = spec.get("args", {})
            # Grader calls must not pollute the measured call list.
            snapshot = list(self.bridge.calls)
            record = await self.bridge.call(tool, arguments)
            self.bridge.calls = snapshot

            if not record.ok and not spec.get("expect_error"):
                results.append(
                    CheckResult(f"{tool}({arguments}) succeeds", False, record.error or "tool failed")
                )
                continue
            try:
                payload = json.loads(record.result_text)
            except json.JSONDecodeError:
                results.append(CheckResult(f"{tool} returns JSON", False, record.result_text[:200]))
                continue

            for expectation in spec.get("expect", []):
                path = expectation.get("path", "")
                actual = dig(payload, path) if path else payload
                passed, detail = compare(actual, expectation)
                label = ", ".join(
                    f"{key}={value}" for key, value in expectation.items() if key != "path"
                )
                results.append(CheckResult(f"{tool}.{path} {label}", passed, "" if passed else detail))

        for expectation in task.answer_checks:
            passed, detail = compare(answer, expectation)
            label = ", ".join(f"{key}={value}" for key, value in expectation.items())
            results.append(CheckResult(f"answer {label}", passed, "" if passed else detail))

        self.results = results
        return results
