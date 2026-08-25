"""Mock ITSM backend backed by :class:`snow_mcp.store.ITSMStore`.

This is the default backend: deterministic, offline, zero-credential, and
resettable between eval cases. It enforces the same validation rules a real
ServiceNow instance would, because an agent that only ever sees a permissive
API learns habits that break in production.
"""

from __future__ import annotations

from typing import Any

from ..store import (
    CLOSE_CODES,
    PRIORITY_LABELS,
    STATE_LABELS,
    ITSMStore,
    RecordNotFound,
    ValidationError,
    normalise_state,
)
from .base import BackendError, ReadOnlyError, shape_incident, summarise_incident

VALID_CATEGORIES = ("hardware", "software", "network", "database", "inquiry")
WRITEABLE_FIELDS = (
    "short_description",
    "description",
    "caller_id",
    "category",
    "subcategory",
    "impact",
    "urgency",
    "state",
    "assignment_group",
    "assigned_to",
    "cmdb_ci",
)
# The MCP tool layer exposes friendlier argument names than the platform's
# column names; normalise them here so both spellings are accepted.
FIELD_ALIASES = {
    "caller": "caller_id",
    "ci": "cmdb_ci",
    "configuration_item": "cmdb_ci",
    "group": "assignment_group",
    "assignee": "assigned_to",
    "title": "short_description",
}


class MockBackend:
    """In-process implementation of :class:`~snow_mcp.backends.base.ITSMBackend`."""

    name = "mock"

    def __init__(self, store: ITSMStore | None = None, *, read_only: bool = False, actor: str = "claude.agent"):
        self.store = store or ITSMStore()
        self.read_only = read_only
        self.actor = actor

    # ------------------------------------------------------------- validation

    def _guard_write(self) -> None:
        if self.read_only:
            raise ReadOnlyError(
                "This MCP server is running in read-only mode; write tools are disabled. "
                "Report the intended change to the user instead of attempting it."
            )

    def _resolve_reference(self, table: str, value: str | None, label: str) -> str:
        """Reference fields must point at a record that actually exists."""
        if not value:
            return ""
        record = self.store.find(table, value)
        if record is None:
            options = [
                row.get("name") or row.get("user_name")
                for row in self.store.all(table)[:12]
            ]
            raise ValidationError(
                f"{label} {value!r} does not exist. Known values include: {', '.join(filter(None, options))}"
            )
        return record.get("name") or record.get("user_name") or value

    def _validate_incident_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, value in fields.items():
            key = FIELD_ALIASES.get(key, key)
            if value is None or value == "":
                continue
            if key not in WRITEABLE_FIELDS:
                if key == "priority":
                    raise ValidationError(
                        "priority is derived from impact and urgency and cannot be set directly. "
                        f"Set impact and urgency instead (matrix: {PRIORITY_LABELS})."
                    )
                raise ValidationError(f"{key!r} is not a writeable incident field. Writeable: {WRITEABLE_FIELDS}")
            if key in ("impact", "urgency"):
                text = str(value).strip()[:1]
                if text not in ("1", "2", "3"):
                    raise ValidationError(f"{key} must be 1 (high), 2 (medium) or 3 (low); got {value!r}")
                clean[key] = text
            elif key == "state":
                state = normalise_state(value)
                if state is None:
                    raise ValidationError(
                        f"Unknown state {value!r}. Valid states: "
                        + ", ".join(f"{code}={label}" for code, label in STATE_LABELS.items())
                    )
                clean[key] = state
            elif key == "category":
                text = str(value).strip().lower()
                if text not in VALID_CATEGORIES:
                    raise ValidationError(f"category must be one of {VALID_CATEGORIES}; got {value!r}")
                clean[key] = text
            elif key == "caller_id":
                clean[key] = self._resolve_reference("sys_user", value, "caller_id")
            elif key == "assigned_to":
                clean[key] = self._resolve_reference("sys_user", value, "assigned_to")
            elif key == "assignment_group":
                clean[key] = self._resolve_reference("sys_user_group", value, "assignment_group")
            elif key == "cmdb_ci":
                clean[key] = self._resolve_reference("cmdb_ci", value, "cmdb_ci")
            else:
                clean[key] = str(value)
        return clean

    # -------------------------------------------------------------- incidents

    def create_incident(self, **fields: Any) -> dict[str, Any]:
        self._guard_write()
        if not fields.get("short_description"):
            raise ValidationError("short_description is required to create an incident")
        clean = self._validate_incident_fields(fields)
        clean.setdefault("impact", "3")
        clean.setdefault("urgency", "3")
        clean.setdefault("state", "1")
        clean.setdefault("category", "inquiry")
        clean.setdefault("assignment_group", "Service Desk")
        clean.setdefault("assigned_to", "")
        clean["opened_by"] = self.actor
        record = self.store.insert("incident", clean)
        return shape_incident(record)

    def get_incident(self, number: str) -> dict[str, Any]:
        try:
            record = self.store.get("incident", number)
        except RecordNotFound as exc:
            raise BackendError(
                f"No incident matches {number!r}. Use search_incidents to find the correct number "
                "rather than guessing one."
            ) from exc
        shaped = shape_incident(record)
        shaped["age_hours"] = self.store.age_in_hours(record)
        return shaped

    def search_incidents(self, encoded_query: str, limit: int, order_by: str | None = None) -> list[dict[str, Any]]:
        ordering = None
        if order_by:
            descending = order_by.startswith("-")
            ordering = [(order_by.lstrip("-"), descending)]
        rows = self.store.query("incident", encoded_query, limit=limit)
        if ordering:
            from ..query import apply as apply_query

            rows = apply_query(rows, None, order_by=ordering)
        return [summarise_incident(row) for row in rows]

    def update_incident(self, number: str, patch: dict[str, Any], work_note: str | None = None) -> dict[str, Any]:
        self._guard_write()
        current = self.store.find("incident", number)
        if current is None:
            raise BackendError(f"No incident matches {number!r}; nothing was updated.")
        if current.get("state") == "7":
            raise ValidationError(
                f"{current['number']} is Closed and cannot be modified. "
                "Closed incidents require a new incident rather than an update."
            )
        clean = self._validate_incident_fields(patch)
        if not clean and not work_note:
            raise ValidationError("update_incident needs at least one field to change or a work_note to add")
        if clean:
            self.store.update("incident", number, clean)
        if work_note:
            self.store.append_journal("incident", number, "work_notes", work_note, self.actor)
        return shape_incident(self.store.get("incident", number))

    def add_comment(self, number: str, comment: str) -> dict[str, Any]:
        self._guard_write()
        if not comment.strip():
            raise ValidationError("comment text cannot be empty")
        if self.store.find("incident", number) is None:
            raise BackendError(f"No incident matches {number!r}; no comment was added.")
        record = self.store.append_journal("incident", number, "comments", comment, self.actor)
        return shape_incident(record)

    def resolve_incident(self, number: str, close_code: str, close_notes: str) -> dict[str, Any]:
        self._guard_write()
        current = self.store.find("incident", number)
        if current is None:
            raise BackendError(f"No incident matches {number!r}; nothing was resolved.")
        if current.get("state") in ("6", "7"):
            raise ValidationError(
                f"{current['number']} is already {STATE_LABELS[current['state']]} "
                f"(closed on {current.get('resolved_at') or 'an earlier date'})."
            )
        if close_code not in CLOSE_CODES:
            raise ValidationError(f"close_code must be one of {CLOSE_CODES}; got {close_code!r}")
        if len(close_notes.strip()) < 10:
            raise ValidationError(
                "close_notes must describe what actually fixed the issue (at least 10 characters)."
            )
        stamp = self.store.clock.stamp()
        record = self.store.update(
            "incident",
            number,
            {"state": "6", "close_code": close_code, "close_notes": close_notes, "resolved_at": stamp},
        )
        return shape_incident(record)

    def similar_incidents(self, text: str, cmdb_ci: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        return self.store.similar_incidents(text, cmdb_ci=cmdb_ci, limit=limit)

    def aggregate_incidents(self, group_by: str, encoded_query: str = "") -> list[dict[str, Any]]:
        allowed = ("assignment_group", "priority", "state", "category", "assigned_to", "cmdb_ci")
        if group_by not in allowed:
            raise ValidationError(f"group_by must be one of {allowed}")
        return self.store.aggregate_incidents(group_by, encoded_query or None)

    # -------------------------------------------------------------- knowledge

    def search_knowledge(self, text: str, category: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        return self.store.search_knowledge(text, category=category, limit=limit)

    def get_article(self, number: str) -> dict[str, Any]:
        try:
            article = self.store.get("kb_knowledge", number)
        except RecordNotFound as exc:
            raise BackendError(
                f"No knowledge article matches {number!r}. Use search_knowledge first."
            ) from exc
        return {
            "number": article["number"],
            "short_description": article["short_description"],
            "category": article.get("category", ""),
            "keywords": article.get("keywords", ""),
            "workflow_state": article.get("workflow_state", ""),
            "view_count": article.get("view_count", ""),
            "text": article.get("text", ""),
        }

    # ------------------------------------------------------------------- cmdb

    def search_cmdb(self, encoded_query: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.store.query("cmdb_ci", encoded_query, limit=limit)
        return [self._shape_ci(row, verbose=False) for row in rows]

    def get_ci(self, name: str) -> dict[str, Any]:
        try:
            record = self.store.get("cmdb_ci", name)
        except RecordNotFound as exc:
            raise BackendError(
                f"No configuration item matches {name!r}. Use search_cmdb to find the correct name."
            ) from exc
        shaped = self._shape_ci(record)
        open_incidents = self.store.query(
            "incident", f"cmdb_ci={record['name']}^active=true", limit=10
        )
        shaped["open_incidents"] = [summarise_incident(row) for row in open_incidents]
        shaped["open_incident_count"] = len(open_incidents)
        return shaped

    def ci_relationships(self, name: str, direction: str = "both", depth: int = 2) -> dict[str, Any]:
        record = self.store.find("cmdb_ci", name)
        if record is None:
            raise BackendError(f"No configuration item matches {name!r}.")
        if direction not in ("both", "upstream", "downstream"):
            raise ValidationError("direction must be 'both', 'upstream' or 'downstream'")
        depth = max(1, min(int(depth), 5))
        graph = self.store.related_cis(record["name"], direction=direction, depth=depth)
        graph["ci"] = record["name"]
        graph["depth"] = depth
        graph["legend"] = {
            "upstream": "Configuration items this CI depends on - a failure there causes a failure here.",
            "downstream": "Configuration items that depend on this CI - these are impacted if this CI fails.",
        }
        return graph

    @staticmethod
    def _shape_ci(record: dict[str, Any], *, verbose: bool = True) -> dict[str, Any]:
        status_labels = {"1": "Operational", "2": "Non-Operational", "3": "Degraded", "6": "Retired"}
        shaped = {
            "name": record.get("name", ""),
            "sys_class_name": record.get("sys_class_name", ""),
            "short_description": record.get("short_description", ""),
            "operational_status": status_labels.get(
                str(record.get("operational_status", "")), record.get("operational_status", "")
            ),
            "environment": record.get("environment", ""),
            "support_group": record.get("support_group", ""),
            "business_criticality": record.get("business_criticality", ""),
        }
        if verbose:
            shaped.update({
                "owned_by": record.get("owned_by", ""),
                "location": record.get("location", ""),
                "ip_address": record.get("ip_address", ""),
                "sys_id": record.get("sys_id", ""),
            })
        return shaped

    # ----------------------------------------------------------------- people

    def lookup_user(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        needle = (query or "").strip().lower()
        results = []
        for user in self.store.all("sys_user"):
            haystack = " ".join(
                str(user.get(field, ""))
                for field in ("name", "user_name", "email", "department", "title")
            ).lower()
            if needle and needle not in haystack:
                continue
            results.append({
                "name": user.get("name", ""),
                "user_name": user.get("user_name", ""),
                "email": user.get("email", ""),
                "department": user.get("department", ""),
                "title": user.get("title", ""),
                "vip": user.get("vip", "false"),
                "manager": user.get("manager", ""),
                "location": user.get("location", ""),
            })
        return results[:limit]

    # ------------------------------------------------------------------ admin

    def reset(self) -> None:
        """Restore the fixture. Used between eval cases."""
        self.store.reset()
