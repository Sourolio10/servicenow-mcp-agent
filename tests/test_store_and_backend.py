import pytest

from snow_mcp.backends.base import BackendError, ReadOnlyError
from snow_mcp.backends.mock import MockBackend
from snow_mcp.store import ITSMStore, RecordNotFound, ValidationError, derive_priority


@pytest.fixture
def backend():
    return MockBackend(store=ITSMStore())


# ------------------------------------------------------------------- store


def test_seed_loads_all_tables():
    store = ITSMStore()
    assert len(store.all("incident")) == 16
    assert len(store.all("kb_knowledge")) == 8
    assert len(store.all("cmdb_ci")) == 13
    assert len(store.all("sys_user")) == 10


def test_priority_matrix_matches_servicenow_defaults():
    assert derive_priority("1", "1") == "1"
    assert derive_priority("2", "2") == "3"
    assert derive_priority("3", "3") == "5"


def test_priority_is_recalculated_on_update():
    store = ITSMStore()
    store.update("incident", "INC0010005", {"impact": "1", "urgency": "1"})
    assert store.get("incident", "INC0010005")["priority"] == "1"


def test_number_generation_is_sequential():
    store = ITSMStore()
    created = store.insert("incident", {"short_description": "test"})
    assert created["number"] == "INC0010017"
    assert store.insert("incident", {"short_description": "again"})["number"] == "INC0010018"


def test_active_flag_follows_state():
    store = ITSMStore()
    assert store.get("incident", "INC0010001")["active"] == "true"
    store.update("incident", "INC0010001", {"state": "6"})
    assert store.get("incident", "INC0010001")["active"] == "false"


def test_journal_fields_are_append_only():
    store = ITSMStore()
    store.append_journal("incident", "INC0010013", "work_notes", "first", "tester")
    record = store.append_journal("incident", "INC0010013", "work_notes", "second", "tester")
    assert [entry["value"] for entry in record["work_notes"]] == ["first", "second"]


def test_reset_discards_changes():
    store = ITSMStore()
    store.update("incident", "INC0010013", {"state": "2"})
    store.reset()
    assert store.get("incident", "INC0010013")["state"] == "1"
    assert store.audit == []


def test_find_by_sys_id_number_and_name():
    store = ITSMStore()
    record = store.get("incident", "INC0010001")
    assert store.find("incident", record["sys_id"])["number"] == "INC0010001"
    assert store.find("cmdb_ci", "PAY-DB-01")["support_group"] == "Database Administration"
    with pytest.raises(RecordNotFound):
        store.get("incident", "INC9999999")


def test_multi_hop_relationship_walk():
    store = ITSMStore()
    graph = store.related_cis("SAN-ARRAY-01", direction="downstream", depth=3)
    names = {item["name"]: item["hops"] for item in graph["downstream"]}
    assert names["ESX-CLUSTER-A"] == 1
    assert names["PAY-APP-01"] == 3  # SAN -> cluster -> db/web -> app


def test_relationship_depth_is_respected():
    store = ITSMStore()
    shallow = store.related_cis("SAN-ARRAY-01", direction="downstream", depth=1)
    assert [item["name"] for item in shallow["downstream"]] == ["ESX-CLUSTER-A"]


# ----------------------------------------------------------------- backend


def test_create_incident_defaults_and_derived_priority(backend):
    created = backend.create_incident(short_description="Projector will not power on", caller="Tom Becker")
    assert created["state_label"] == "New"
    assert created["priority"] == "5"  # impact 3 x urgency 3
    assert created["caller_id"] == "Tom Becker"


def test_create_incident_rejects_unknown_reference(backend):
    with pytest.raises(ValidationError, match="does not exist"):
        backend.create_incident(short_description="test", caller_id="Imaginary Person")


def test_priority_cannot_be_set_directly(backend):
    with pytest.raises(ValidationError, match="derived"):
        backend.create_incident(short_description="test", priority="1")


def test_update_rejects_invalid_category(backend):
    with pytest.raises(ValidationError, match="category"):
        backend.update_incident("INC0010013", {"category": "banana"}, None)


def test_closed_incident_cannot_be_updated(backend):
    with pytest.raises(ValidationError, match="Closed"):
        backend.update_incident("INC0010010", {}, "still working")


def test_resolve_requires_valid_close_code(backend):
    with pytest.raises(ValidationError, match="close_code"):
        backend.resolve_incident("INC0010013", "Fixed it", "the printer was unplugged")


def test_resolve_requires_meaningful_notes(backend):
    with pytest.raises(ValidationError, match="close_notes"):
        backend.resolve_incident("INC0010013", "Solved (Permanently)", "ok")


def test_resolve_is_idempotent_guarded(backend):
    backend.resolve_incident("INC0010013", "Solved (Permanently)", "Power cable reseated at the wall.")
    with pytest.raises(ValidationError, match="already"):
        backend.resolve_incident("INC0010013", "Solved (Permanently)", "Power cable reseated again.")


def test_unknown_incident_raises_backend_error(backend):
    with pytest.raises(BackendError, match="No incident"):
        backend.get_incident("INC0099999")


def test_read_only_mode_blocks_writes():
    backend = MockBackend(store=ITSMStore(), read_only=True)
    with pytest.raises(ReadOnlyError):
        backend.create_incident(short_description="nope")
    # reads still work
    assert backend.get_incident("INC0010001")["number"] == "INC0010001"


def test_work_note_and_comment_go_to_different_journals(backend):
    backend.update_incident("INC0010013", {}, "internal triage note")
    backend.add_comment("INC0010013", "We are looking into this for you.")
    record = backend.get_incident("INC0010013")
    assert [entry["value"] for entry in record["work_notes"]] == ["internal triage note"]
    assert [entry["value"] for entry in record["comments"]] == ["We are looking into this for you."]


def test_similar_incidents_prefers_resolved_lookalikes(backend):
    matches = backend.similar_incidents("checkout gateway timeouts", None, 3)
    assert matches[0]["number"] == "INC0010011"
    assert "connection pool" in matches[0]["close_notes"].lower()


def test_knowledge_search_ranks_by_title_and_keywords(backend):
    articles = backend.search_knowledge("vpn authentication after password change", None, 3)
    assert articles[0]["number"] == "KB0000004"


def test_get_ci_includes_open_incidents(backend):
    ci = backend.get_ci("VPN-GW-01")
    assert ci["open_incident_count"] == 2
    assert {item["number"] for item in ci["open_incidents"]} == {"INC0010003", "INC0010004"}


def test_aggregate_rejects_unknown_group_by(backend):
    with pytest.raises(ValidationError):
        backend.aggregate_incidents("favourite_colour", "")


def test_lookup_user_matches_partial_and_department(backend):
    assert backend.lookup_user("dana", 5)[0]["name"] == "Dana Whitfield"
    assert {u["name"] for u in backend.lookup_user("Service Desk", 5)} == {"Jorge Alvarez", "Carlos Mendes"}
