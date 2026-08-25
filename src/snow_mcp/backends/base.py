"""The contract every ITSM backend implements.

The MCP tool layer never touches a store or an HTTP client directly. It calls
this interface, which means the exact same 14 tools run against the bundled
mock fixture or a live ServiceNow Personal Developer Instance with one
environment variable changed.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..store import PRIORITY_LABELS, STATE_LABELS

# Fields returned for an incident in list context. Keeping this tight matters:
# every extra field is multiplied by every search result in the agent's context.
INCIDENT_LIST_FIELDS = (
    "number",
    "short_description",
    "state_label",
    "priority_label",
    "assignment_group",
    "assigned_to",
    "caller_id",
    "cmdb_ci",
    "opened_at",
)


class BackendError(RuntimeError):
    """Raised for backend failures that should surface to the agent as text."""


class ReadOnlyError(BackendError):
    """Raised when a write is attempted while the server is in read-only mode."""


def shape_incident(record: dict[str, Any], *, verbose: bool = True) -> dict[str, Any]:
    """Normalise a raw incident into the shape the agent sees.

    Adds human-readable labels alongside the numeric codes so the model does
    not have to remember that state 6 means Resolved.
    """
    shaped: dict[str, Any] = {
        "number": record.get("number", ""),
        "short_description": record.get("short_description", ""),
        "state": record.get("state", ""),
        "state_label": STATE_LABELS.get(str(record.get("state", "")), str(record.get("state", ""))),
        "priority": record.get("priority", ""),
        "priority_label": PRIORITY_LABELS.get(str(record.get("priority", "")), ""),
        "impact": record.get("impact", ""),
        "urgency": record.get("urgency", ""),
        "assignment_group": record.get("assignment_group", ""),
        "assigned_to": record.get("assigned_to", ""),
        "caller_id": record.get("caller_id", ""),
        "cmdb_ci": record.get("cmdb_ci", ""),
        "category": record.get("category", ""),
        "subcategory": record.get("subcategory", ""),
        "opened_at": record.get("opened_at", ""),
        "sys_updated_on": record.get("sys_updated_on", ""),
        "active": record.get("active", ""),
    }
    if verbose:
        shaped.update({
            "description": record.get("description", ""),
            "resolved_at": record.get("resolved_at", ""),
            "close_code": record.get("close_code", ""),
            "close_notes": record.get("close_notes", ""),
            "work_notes": record.get("work_notes", []),
            "comments": record.get("comments", []),
            "sys_id": record.get("sys_id", ""),
        })
    return shaped


def summarise_incident(record: dict[str, Any]) -> dict[str, Any]:
    shaped = shape_incident(record, verbose=False)
    return {name: shaped.get(name, "") for name in INCIDENT_LIST_FIELDS}


@runtime_checkable
class ITSMBackend(Protocol):
    """Operations the MCP server exposes as tools."""

    name: str
    read_only: bool

    # incidents
    def create_incident(self, **fields: Any) -> dict[str, Any]: ...
    def get_incident(self, number: str) -> dict[str, Any]: ...
    def search_incidents(self, encoded_query: str, limit: int, order_by: str | None) -> list[dict[str, Any]]: ...
    def update_incident(self, number: str, patch: dict[str, Any], work_note: str | None) -> dict[str, Any]: ...
    def add_comment(self, number: str, comment: str) -> dict[str, Any]: ...
    def resolve_incident(self, number: str, close_code: str, close_notes: str) -> dict[str, Any]: ...
    def similar_incidents(self, text: str, cmdb_ci: str | None, limit: int) -> list[dict[str, Any]]: ...
    def aggregate_incidents(self, group_by: str, encoded_query: str) -> list[dict[str, Any]]: ...

    # knowledge
    def search_knowledge(self, text: str, category: str | None, limit: int) -> list[dict[str, Any]]: ...
    def get_article(self, number: str) -> dict[str, Any]: ...

    # cmdb
    def search_cmdb(self, encoded_query: str, limit: int) -> list[dict[str, Any]]: ...
    def get_ci(self, name: str) -> dict[str, Any]: ...
    def ci_relationships(self, name: str, direction: str, depth: int) -> dict[str, Any]: ...

    # people
    def lookup_user(self, query: str, limit: int) -> list[dict[str, Any]]: ...
