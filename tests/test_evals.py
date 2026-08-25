"""Eval harness tests: the metrics must be right before the numbers mean anything."""

import pytest

from snow_mcp.config import Settings
from snow_mcp.evals.checks import compare, dig
from snow_mcp.evals.metrics import LatencyStats, aggregate, percentile, score_tool_selection
from snow_mcp.evals.report import to_html, to_markdown
from snow_mcp.evals.runner import EvalRunner, load_tasks, summarise

# --------------------------------------------------------------- metrics


def test_perfect_selection_scores_one():
    score = score_tool_selection(["get_incident"], ["get_incident"])
    assert score.precision == score.recall == score.f1 == 1.0
    assert score.exact_match and score.first_tool_correct


def test_missing_tool_reduces_recall_only():
    score = score_tool_selection(["get_incident"], ["get_incident", "search_knowledge"])
    assert score.precision == 1.0
    assert score.recall == 0.5
    assert score.missing == ["search_knowledge"]


def test_extra_tool_reduces_precision_only():
    score = score_tool_selection(["get_incident", "search_cmdb"], ["get_incident"])
    assert score.precision == 0.5
    assert score.recall == 1.0
    assert score.extra == ["search_cmdb"]


def test_optional_tools_are_not_penalised():
    score = score_tool_selection(
        ["get_incident", "lookup_user"], ["get_incident"], optional=["lookup_user"]
    )
    assert score.precision == 1.0
    assert score.extra == []
    assert score.exact_match


def test_repeated_calls_count_once():
    score = score_tool_selection(["get_incident"] * 5, ["get_incident"])
    assert score.precision == 1.0
    assert score.called == ["get_incident"]


def test_forbidden_tool_is_flagged():
    score = score_tool_selection(
        ["find_similar_incidents", "create_incident"],
        ["find_similar_incidents"],
        forbidden=["create_incident"],
    )
    assert score.forbidden_used == ["create_incident"]


def test_first_tool_accuracy_uses_allowed_first():
    score = score_tool_selection(
        ["search_incidents", "get_incident_stats"],
        ["get_incident_stats"],
        optional=["search_incidents"],
        allowed_first=["get_incident_stats"],
    )
    assert not score.first_tool_correct


def test_no_tools_expected_and_none_called_is_correct():
    score = score_tool_selection([], [])
    assert score.f1 == 1.0 and score.first_tool_correct


def test_percentiles_and_latency_stats():
    values = [10.0, 20.0, 30.0, 40.0, 100.0]
    assert percentile(values, 0.5) == 30.0
    assert percentile(values, 0.95) == 100.0
    assert percentile([], 0.5) == 0.0
    stats = LatencyStats.of(values)
    assert stats.count == 5 and stats.max_ms == 100.0 and stats.mean_ms == 40.0


# ---------------------------------------------------------------- checks


def test_dig_walks_dicts_lists_and_wildcards():
    payload = {"incidents": [{"number": "A", "tags": ["x"]}, {"number": "B"}]}
    assert dig(payload, "incidents.0.number") == "A"
    assert dig(payload, "incidents.*") == payload["incidents"]
    assert dig(payload, "incidents.number") == ["A", "B"]
    assert dig(payload, "nope.deeper") is None


@pytest.mark.parametrize(
    "actual,expectation,expected",
    [
        ("6", {"equals": "6"}, True),
        ("6", {"equals": "2"}, False),
        (["a", "b"], {"contains": "B"}, True),
        (["a"], {"contains_all": ["a", "z"]}, False),
        (["a", "z"], {"contains_any": ["q", "z"]}, True),
        ("hello", {"not_contains": "bye"}, True),
        ("INC0010042", {"matches": r"INC00\d{5}"}, True),
        ("call me on 555 123 4567", {"not_matches": r"\d[\d ().-]{8,}\d"}, False),
        ([1, 2, 3], {"min_length": 2}, True),
        ([], {"length": 0}, True),
        ("5", {"gte": 4}, True),
        ("5", {"lte": 4}, False),
    ],
)
def test_compare_operators(actual, expectation, expected):
    passed, _ = compare(actual, expectation)
    assert passed is expected


# ------------------------------------------------------------- task suite


def test_suite_loads_and_is_internally_consistent():
    from snow_mcp.backends.mock import MockBackend
    from snow_mcp.server import build_server
    from snow_mcp.store import ITSMStore

    tasks = load_tasks()
    assert len(tasks) == 24
    assert len({task.id for task in tasks}) == len(tasks)

    server = build_server(backend=MockBackend(store=ITSMStore()), settings=Settings())
    known = set(server._tool_manager._tools)  # noqa: SLF001 - deliberate introspection
    for task in tasks:
        declared = set(task.expected_tools) | set(task.optional_tools) | set(task.forbidden_tools)
        assert declared <= known, f"{task.id} references unknown tools: {declared - known}"
        assert not (set(task.expected_tools) & set(task.forbidden_tools)), task.id
        assert task.state_checks or task.answer_checks, f"{task.id} has no graded checks"
        for check in task.state_checks:
            assert check["tool"] in known, f"{task.id} grades with unknown tool {check['tool']}"


def test_filtering_by_id_and_category():
    assert [task.id for task in load_tasks(only=["kb-vpn-password"])] == ["kb-vpn-password"]
    assert {task.category for task in load_tasks(categories=["cmdb"])} == {"cmdb"}


# ---------------------------------------------------- end-to-end (scripted)


def _scripted_factory(turns):
    from snow_mcp.agent.llm import ScriptedLLM

    return lambda: ScriptedLLM(list(turns))


async def test_end_to_end_eval_run_passes_a_task():
    """A scripted 'perfect' agent must score 1.0 on a real task, checks included."""
    from snow_mcp.agent.llm import ScriptedTurn

    task = next(t for t in load_tasks(only=["triage-assign-printer"]))
    turns = [
        ScriptedTurn(tool_calls=[("update_incident", {
            "number": "INC0010013",
            "assigned_to": "Jorge Alvarez",
            "state": "In Progress",
            "work_note": "Checked the print queue; the device is not responding to ping.",
        })]),
        ScriptedTurn(text="INC0010013 assigned to Jorge Alvarez and moved to In Progress."),
    ]
    runner = EvalRunner(_scripted_factory(turns), settings=Settings())
    results = await runner.run_suite([task])
    result = results[0]
    assert result.completed, [c.to_dict() for c in result.failed_checks]
    assert result.selection.f1 == 1.0
    assert all(check.passed for check in result.checks)


async def test_end_to_end_eval_run_fails_a_lying_agent():
    """An agent that claims success without changing state must not pass."""
    from snow_mcp.agent.llm import ScriptedTurn

    task = next(t for t in load_tasks(only=["triage-assign-printer"]))
    turns = [ScriptedTurn(text="Done! I've assigned INC0010013 to Jorge Alvarez.")]
    runner = EvalRunner(_scripted_factory(turns), settings=Settings())
    result = (await runner.run_suite([task]))[0]
    assert not result.completed
    assert result.selection.recall == 0.0
    assert result.failed_checks


async def test_forbidden_tool_use_fails_the_task():
    from snow_mcp.agent.llm import ScriptedTurn

    task = next(t for t in load_tasks(only=["create-duplicate-avoidance"]))
    turns = [
        ScriptedTurn(tool_calls=[("find_similar_incidents", {"problem_description": "502 checkout"})]),
        ScriptedTurn(tool_calls=[("create_incident", {"short_description": "Checkout 502 errors"})]),
        ScriptedTurn(text="Logged a new incident. Related to INC0010001, already existing."),
    ]
    runner = EvalRunner(_scripted_factory(turns), settings=Settings())
    result = (await runner.run_suite([task]))[0]
    assert result.selection.forbidden_used == ["create_incident"]
    assert not result.completed


async def test_tasks_are_isolated_from_each_other():
    """Resolving an incident in one task must not leak into the next."""
    from snow_mcp.agent.llm import ScriptedTurn

    tasks = load_tasks(only=["resolve-vpn-with-kb", "cmdb-ci-open-incidents"])
    assert len(tasks) == 2
    turns = [
        ScriptedTurn(tool_calls=[("get_ci", {"name": "VPN-GW-01"})]),
        ScriptedTurn(text="INC0010003 and INC0010004 are open on VPN-GW-01."),
    ]
    runner = EvalRunner(_scripted_factory(turns), settings=Settings())
    results = await runner.run_suite(tasks, concurrency=2)
    by_id = {result.task.id: result for result in results}
    assert by_id["cmdb-ci-open-incidents"].completed


async def test_report_generation_from_a_real_run(tmp_path):
    from snow_mcp.agent.llm import ScriptedTurn

    task = next(t for t in load_tasks(only=["lookup-incident-detail"]))
    turns = [
        ScriptedTurn(tool_calls=[("get_incident", {"number": "INC0010009"})]),
        ScriptedTurn(text="INC0010009 is In Progress and assigned to Ravi Patel."),
    ]
    runner = EvalRunner(_scripted_factory(turns), settings=Settings(), trace_path=tmp_path / "traces.jsonl")
    results = await runner.run_suite([task])
    report = summarise(results, metadata={"model": "scripted", "prompt_variant": "operator"})

    assert report["summary"]["task_completion"]["rate"] == 1.0
    assert report["summary"]["tool_selection"]["macro_f1"] == 1.0
    assert report["summary"]["latency"]["per_tool_call_ms"]["count"] == 1
    assert "get_incident" in report["summary"]["latency"]["by_tool"]

    markdown = to_markdown(report)
    assert "Task completion rate" in markdown and "lookup-incident-detail" in markdown
    html = to_html(report)
    assert html.startswith("<!doctype html>") and "PASS" in html
    assert (tmp_path / "traces.jsonl").read_text().count("\n") == 1


def test_aggregate_on_empty_results_is_safe():
    assert aggregate([]) == {}
