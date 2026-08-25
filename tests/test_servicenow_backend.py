"""The live ServiceNow backend, exercised against the bundled ASGI mock.

This is the test that keeps the second backend honest. It drives
:class:`ServiceNowBackend` — the same class that talks to a real Personal
Developer Instance — over httpx's ASGI transport against the FastAPI app, so
the Table API request shaping, auth, display-value flattening and error mapping
are all covered without a network call or a PDI.
"""

import pytest
from starlette.testclient import TestClient

from snow_mcp.backends.base import BackendError
from snow_mcp.backends.servicenow import ServiceNowBackend
from snow_mcp.mock_api.app import app, store


def _client(username: str = "admin", password: str = "admin") -> TestClient:
    """A sync httpx client wired straight into the ASGI app - no socket, no PDI."""
    client = TestClient(app, base_url="http://testserver", headers={"Accept": "application/json"})
    client.auth = (username, password)
    return client


@pytest.fixture
def backend():
    store.reset()
    return ServiceNowBackend("http://testserver", "admin", "admin", client=_client())


def test_health_endpoint_reports_table_sizes():
    payload = TestClient(app).get("/health").json()
    assert payload["status"] == "ok"
    assert payload["tables"]["incident"] >= 16


def test_bad_credentials_map_to_a_readable_error():
    backend = ServiceNowBackend(
        "http://testserver", "admin", "wrong", client=_client("admin", "wrong")
    )
    with pytest.raises(BackendError, match="credentials"):
        backend.get_incident("INC0010001")


def test_get_incident_flattens_display_values(backend):
    record = backend.get_incident("INC0010001")
    assert record["number"] == "INC0010001"
    assert record["assigned_to"] == "Priya Nair"      # flattened, not a sys_id
    assert record["state_label"] == "In Progress"     # label added by the shaping layer
    assert isinstance(record["cmdb_ci"], str)


def test_search_incidents_forwards_the_encoded_query(backend):
    rows = backend.search_incidents("active=true^priority<=2", 10, None)
    assert {row["number"] for row in rows} == {
        "INC0010001", "INC0010002", "INC0010009", "INC0010015"
    }


def test_search_incidents_applies_order_by(backend):
    rows = backend.search_incidents("active=true", 5, "-opened_at")
    stamps = [row["opened_at"] for row in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_create_then_patch_round_trip(backend):
    created = backend.create_incident(
        short_description="Projector will not power on", caller_id="Tom Becker", category="hardware"
    )
    number = created["number"]
    assert number.startswith("INC")

    backend.update_incident(number, {"state": "In Progress"}, work_note="Checked the power cable.")
    fetched = backend.get_incident(number)
    assert fetched["state_label"] == "In Progress"
    assert fetched["work_notes"][-1]["value"] == "Checked the power cable."


def test_priority_is_never_sent_to_the_platform(backend):
    """Priority is derived server-side; the client must strip any attempt to set it."""
    created = backend.create_incident(
        short_description="Derived priority check", impact="1", urgency="1", priority="5"
    )
    assert created["priority"] == "1"


def test_comments_and_work_notes_land_in_different_journals(backend):
    backend.add_comment("INC0010013", "We are looking into this for you.")
    backend.update_incident("INC0010013", {}, work_note="Internal: printer not responding to ping.")
    record = backend.get_incident("INC0010013")
    assert [entry["value"] for entry in record["comments"]] == ["We are looking into this for you."]
    assert record["work_notes"][-1]["value"].startswith("Internal:")


def test_resolve_sets_close_fields(backend):
    backend.resolve_incident(
        "INC0010013", "Solved (Permanently)", "Power cable reseated at the wall socket."
    )
    record = backend.get_incident("INC0010013")
    assert record["state_label"] == "Resolved"
    assert record["close_code"] == "Solved (Permanently)"


def test_resolve_rejects_an_invalid_close_code(backend):
    with pytest.raises(BackendError, match="close_code"):
        backend.resolve_incident("INC0010013", "Fixed It", "notes here")


def test_missing_incident_raises_with_guidance(backend):
    with pytest.raises(BackendError, match="search_incidents"):
        backend.get_incident("INC0099999")


def test_cmdb_lookup_and_relationship_walk(backend):
    ci = backend.get_ci("PAY-APP-01")
    assert ci["support_group"] == "Application Support"
    assert ci["open_incident_count"] >= 1

    graph = backend.ci_relationships("PAY-APP-01", "upstream", 3)
    names = {item["name"] for item in graph["upstream"]}
    assert {"PAY-DB-01", "ESX-CLUSTER-A", "SAN-ARRAY-01"} <= names


def test_knowledge_and_user_lookup(backend):
    articles = backend.search_knowledge("vpn", None, 5)
    assert any(article["number"] == "KB0000002" for article in articles)
    assert backend.get_article("KB0000004")["text"]

    users = backend.lookup_user("dana", 5)
    assert users[0]["name"] == "Dana Whitfield"


def test_aggregate_uses_the_stats_endpoint(backend):
    buckets = backend.aggregate_incidents("assignment_group", "active=true")
    assert buckets[0]["group"] == "Service Desk"
    assert buckets[0]["count"] == 6
