"""Protocol-level tests: everything goes through a real MCP client session."""

import json

import pytest
from mcp import Client

from snow_mcp.backends.mock import MockBackend
from snow_mcp.config import Settings
from snow_mcp.server import build_server
from snow_mcp.store import ITSMStore

EXPECTED_TOOLS = {
    "search_incidents", "get_incident", "create_incident", "update_incident",
    "add_incident_comment", "resolve_incident", "find_similar_incidents",
    "get_incident_stats", "search_knowledge", "get_knowledge_article",
    "search_cmdb", "get_ci", "get_ci_relationships", "lookup_user",
}


@pytest.fixture
def server():
    return build_server(backend=MockBackend(store=ITSMStore()), settings=Settings())


async def call(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    text = "\n".join(block.text for block in result.content if getattr(block, "text", None))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


async def test_tool_catalogue_is_complete_and_documented(server):
    async with Client(server) as client:
        listing = await client.list_tools()
        names = {tool.name for tool in listing.tools}
        assert names == EXPECTED_TOOLS
        for tool in listing.tools:
            assert tool.description and len(tool.description) > 120, f"{tool.name} is under-documented"
            assert tool.input_schema["type"] == "object"


async def test_placeholders_are_interpolated_in_descriptions(server):
    async with Client(server) as client:
        listing = await client.list_tools()
        for tool in listing.tools:
            assert "{ceiling}" not in (tool.description or "")
            assert "{close_codes}" not in (tool.description or "")
        resolve = next(tool for tool in listing.tools if tool.name == "resolve_incident")
        assert "Solved (Permanently)" in resolve.description


async def test_required_arguments_are_marked_required(server):
    async with Client(server) as client:
        listing = await client.list_tools()
        create = next(tool for tool in listing.tools if tool.name == "create_incident")
        assert create.input_schema.get("required") == ["short_description"]


async def test_search_incidents_builds_an_encoded_query(server):
    async with Client(server) as client:
        payload = await call(client, "search_incidents", {"text": "vpn", "active_only": True})
        assert payload["count"] == 2
        assert "active=true" in payload["query"]
        assert {i["number"] for i in payload["incidents"]} == {"INC0010003", "INC0010004"}


async def test_search_incidents_honours_encoded_query_escape_hatch(server):
    async with Client(server) as client:
        payload = await call(
            client, "search_incidents", {"encoded_query": "assigned_toISEMPTY^active=true"}
        )
        assert {i["number"] for i in payload["incidents"]} == {
            "INC0010003", "INC0010005", "INC0010013", "INC0010016"
        }


async def test_limit_is_clamped_to_the_configured_ceiling(server):
    async with Client(server) as client:
        payload = await call(client, "search_incidents", {"active_only": False, "limit": 999})
        assert len(payload["incidents"]) <= Settings().max_results


async def test_write_then_read_round_trip(server):
    async with Client(server) as client:
        created = await call(client, "create_incident", {
            "short_description": "Projector in 2A will not power on",
            "caller": "Tom Becker",
            "category": "hardware",
        })
        number = created["number"]
        await call(client, "update_incident", {
            "number": number, "assigned_to": "Jorge Alvarez", "state": "In Progress",
            "work_note": "Checked the power cable.",
        })
        fetched = await call(client, "get_incident", {"number": number})
        assert fetched["state_label"] == "In Progress"
        assert fetched["assigned_to"] == "Jorge Alvarez"
        assert fetched["work_notes"][0]["value"] == "Checked the power cable."


async def test_domain_errors_come_back_as_recoverable_json(server):
    async with Client(server) as client:
        payload = await call(client, "get_incident", {"number": "INC0099999"})
        assert payload["recoverable"] is True
        assert "No incident" in payload["error"]


async def test_update_cannot_resolve_an_incident(server):
    async with Client(server) as client:
        payload = await call(client, "update_incident", {"number": "INC0010013", "state": "Resolved"})
        assert "resolve_incident" in payload["error"]
        state = await call(client, "get_incident", {"number": "INC0010013"})
        assert state["state_label"] == "New"


async def test_resolve_incident_sets_close_fields(server):
    async with Client(server) as client:
        await call(client, "resolve_incident", {
            "number": "INC0010003",
            "close_code": "Solved (Permanently)",
            "close_notes": "Cleared cached credentials per KB0000004 and reconnected.",
        })
        record = await call(client, "get_incident", {"number": "INC0010003"})
        assert record["state_label"] == "Resolved"
        assert record["close_code"] == "Solved (Permanently)"


async def test_relationship_depth_argument_is_honoured(server):
    async with Client(server) as client:
        shallow = await call(client, "get_ci_relationships",
                             {"name": "SAN-ARRAY-01", "direction": "downstream", "depth": 1})
        deep = await call(client, "get_ci_relationships",
                          {"name": "SAN-ARRAY-01", "direction": "downstream", "depth": 3})
        assert len(shallow["downstream"]) == 1
        assert "PAY-APP-01" in {item["name"] for item in deep["downstream"]}


async def test_read_only_mode_disables_writes_over_mcp():
    server = build_server(
        backend=MockBackend(store=ITSMStore(), read_only=True), settings=Settings(read_only=True)
    )
    async with Client(server) as client:
        payload = await call(client, "create_incident", {"short_description": "nope"})
        assert "read-only" in payload["error"]
        assert (await call(client, "get_incident", {"number": "INC0010001"}))["number"] == "INC0010001"


async def test_each_server_gets_isolated_state():
    """Eval isolation depends on this: two servers must not share a store."""
    first = build_server(backend=MockBackend(store=ITSMStore()), settings=Settings())
    second = build_server(backend=MockBackend(store=ITSMStore()), settings=Settings())
    async with Client(first) as client_a:
        await call(client_a, "resolve_incident", {
            "number": "INC0010013", "close_code": "Solved (Permanently)",
            "close_notes": "Reseated the power cable at the wall socket.",
        })
    async with Client(second) as client_b:
        assert (await call(client_b, "get_incident", {"number": "INC0010013"}))["state_label"] == "New"
