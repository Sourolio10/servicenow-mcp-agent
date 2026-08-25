"""Agent loop tests.

These run with no API key and no network: a :class:`ScriptedLLM` replays a fixed
sequence of turns, which lets us assert on the loop's mechanics (message
threading, tool_result plumbing, error recovery, instrumentation) independently
of any model's judgement.
"""

import pytest

from snow_mcp.agent.agent import ITSMAgent
from snow_mcp.agent.bridge import MCPToolBridge
from snow_mcp.agent.llm import ScriptedLLM, ScriptedTurn
from snow_mcp.agent.prompts import get_prompt
from snow_mcp.backends.mock import MockBackend
from snow_mcp.config import Settings
from snow_mcp.server import build_server
from snow_mcp.store import ITSMStore


@pytest.fixture
def server():
    return build_server(backend=MockBackend(store=ITSMStore()), settings=Settings())


async def test_bridge_translates_mcp_tools_to_anthropic_specs(server):
    async with MCPToolBridge(target=server) as bridge:
        specs = bridge.tool_specs
        assert len(specs) == 14
        for spec in specs:
            assert set(spec) == {"name", "description", "input_schema"}
            assert spec["input_schema"]["type"] == "object"
        assert "get_incident" in bridge.tool_names


async def test_bridge_records_latency_and_success(server):
    async with MCPToolBridge(target=server) as bridge:
        record = await bridge.call("get_incident", {"number": "INC0010001"})
        assert record.ok
        assert record.latency_ms > 0
        assert record.sequence == 1
        assert "INC0010001" in record.result_text


async def test_bridge_marks_domain_errors_as_failed_calls(server):
    async with MCPToolBridge(target=server) as bridge:
        record = await bridge.call("get_incident", {"number": "INC0099999"})
        assert not record.ok
        assert "No incident" in (record.error or "")


async def test_agent_single_tool_then_answer(server):
    llm = ScriptedLLM([
        ScriptedTurn(tool_calls=[("get_incident", {"number": "INC0010009"})]),
        ScriptedTurn(text="INC0010009 is In Progress with Ravi Patel."),
    ])
    async with MCPToolBridge(target=server) as bridge:
        run = await ITSMAgent(llm, bridge).run("Status of INC0010009?")
    assert run.tool_sequence == ["get_incident"]
    assert run.stop_reason == "end_turn"
    assert "Ravi Patel" in run.answer
    assert run.total_tool_ms > 0
    assert len(run.turns) == 2


async def test_agent_threads_tool_results_back_to_the_model(server):
    llm = ScriptedLLM([
        ScriptedTurn(tool_calls=[("get_incident", {"number": "INC0010001"})]),
        ScriptedTurn(text="done"),
    ])
    async with MCPToolBridge(target=server) as bridge:
        await ITSMAgent(llm, bridge).run("look it up")
    # third call would have seen: user, assistant(tool_use), user(tool_result)
    second_prompt = llm.seen_messages[1]
    assert second_prompt[1]["role"] == "assistant"
    assert second_prompt[2]["role"] == "user"
    result_block = second_prompt[2]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["is_error"] is False
    assert "Payment service" in result_block["content"]


async def test_agent_recovers_from_a_rejected_tool_call(server):
    """A guardrail error must be surfaced as is_error and be recoverable."""
    llm = ScriptedLLM([
        ScriptedTurn(tool_calls=[("update_incident", {"number": "INC0010005", "state": "Resolved"})]),
        ScriptedTurn(tool_calls=[("update_incident", {"number": "INC0010005", "impact": "1", "urgency": "1"})]),
        ScriptedTurn(text="Raised INC0010005 to priority 1."),
    ])
    async with MCPToolBridge(target=server) as bridge:
        run = await ITSMAgent(llm, bridge).run("make it critical")
        assert len(run.tool_calls) == 2
        assert not run.tool_calls[0].ok
        assert run.tool_calls[1].ok
        record = await bridge.call("get_incident", {"number": "INC0010005"})
    assert '"priority": "1"' in record.result_text


async def test_agent_handles_parallel_tool_calls_in_one_turn(server):
    llm = ScriptedLLM([
        ScriptedTurn(tool_calls=[
            ("get_incident", {"number": "INC0010001"}),
            ("get_ci", {"name": "PAY-APP-01"}),
        ]),
        ScriptedTurn(text="both fetched"),
    ])
    async with MCPToolBridge(target=server) as bridge:
        run = await ITSMAgent(llm, bridge).run("fetch both")
    assert run.tool_sequence == ["get_incident", "get_ci"]
    assert len(run.turns[0].tool_calls) == 2


async def test_agent_stops_at_the_turn_limit(server):
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[("get_incident", {"number": "INC0010001"})])] * 10)
    async with MCPToolBridge(target=server) as bridge:
        run = await ITSMAgent(llm, bridge, max_turns=3).run("loop forever")
    assert run.stop_reason == "max_turns"
    assert len(run.tool_calls) == 3
    assert "3 turns" in (run.error or "")


async def test_run_record_serialises_for_traces(server):
    llm = ScriptedLLM([
        ScriptedTurn(tool_calls=[("search_knowledge", {"text": "vpn"})]),
        ScriptedTurn(text="see KB0000002"),
    ])
    async with MCPToolBridge(target=server) as bridge:
        run = await ITSMAgent(llm, bridge).run("find vpn docs")
    payload = run.to_dict()
    assert payload["tool_sequence"] == ["search_knowledge"]
    assert payload["tool_calls"][0]["latency_ms"] > 0
    assert payload["prompt_variant"] == "operator"


def test_prompt_variants_are_distinct():
    assert len(get_prompt("operator")) > len(get_prompt("minimal")) * 5
    with pytest.raises(ValueError):
        get_prompt("nonexistent")
