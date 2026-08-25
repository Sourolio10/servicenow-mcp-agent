"""Live ServiceNow backend using the REST Table API.

Point this at a free Personal Developer Instance (PDI):

    export SNOW_BACKEND=servicenow
    export SNOW_INSTANCE_URL=https://dev123456.service-now.com
    export SNOW_USERNAME=admin
    export SNOW_PASSWORD='...'

Design notes
------------
* ``sysparm_display_value=all`` is requested so reference fields come back with
  both the GUID and the human label; we flatten to the label. Agents reason far
  better about "Priya Nair" than about a 32-character hex string, and it removes
  a whole class of hallucinated-sys_id failures.
* Every read is capped by ``sysparm_limit`` and an explicit field list, because
  a bare ``/api/now/table/incident`` returns ~180 columns per record and will
  bury the model's context.
* The bundled FastAPI mock speaks this same dialect, so this class can be
  smoke-tested locally against ``http://127.0.0.1:8080`` before touching a PDI.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..store import CLOSE_CODES, PRIORITY_LABELS, STATE_LABELS, normalise_state
from .base import BackendError, ReadOnlyError, shape_incident, summarise_incident

INCIDENT_FIELDS = (
    "number,short_description,description,state,priority,impact,urgency,"
    "assignment_group,assigned_to,caller_id,cmdb_ci,category,subcategory,"
    "opened_at,sys_updated_on,resolved_at,close_code,close_notes,active,sys_id"
)
CI_FIELDS = (
    "name,sys_class_name,short_description,operational_status,environment,"
    "support_group,owned_by,location,ip_address,business_criticality,sys_id"
)
USER_FIELDS = "name,user_name,email,department,title,vip,manager,location,sys_id"
KB_FIELDS = "number,short_description,text,kb_category,workflow_state,sys_view_count,keywords,sys_id"


def _flatten(value: Any) -> Any:
    """Collapse ServiceNow's ``display_value``/``value`` envelope to a string."""
    if isinstance(value, dict):
        return value.get("display_value") or value.get("value") or ""
    return value


def _flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: _flatten(value) for key, value in record.items()}


class ServiceNowBackend:
    """Implementation of the ITSM contract against a real instance."""

    name = "servicenow"

    def __init__(
        self,
        instance_url: str,
        username: str,
        password: str,
        *,
        read_only: bool = False,
        timeout: float = 20.0,
        verify: bool = True,
        client: httpx.Client | None = None,
    ):
        if not instance_url:
            raise BackendError("SNOW_INSTANCE_URL is required for the servicenow backend")
        self.base_url = instance_url.rstrip("/")
        self.read_only = read_only
        if client is not None:
            # Injected transport: used by the test suite to drive the bundled
            # ASGI mock without opening a socket.
            self._client = client
            return
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=(username, password),
            timeout=timeout,
            verify=verify,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    # ------------------------------------------------------------------- http

    def _guard_write(self) -> None:
        if self.read_only:
            raise ReadOnlyError(
                "This MCP server is running in read-only mode; write tools are disabled."
            )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # network, DNS, TLS, timeout
            raise BackendError(f"ServiceNow request failed: {exc}") from exc
        if response.status_code == 401:
            raise BackendError("ServiceNow rejected the credentials (401). Check SNOW_USERNAME/SNOW_PASSWORD.")
        if response.status_code == 403:
            raise BackendError(
                "ServiceNow returned 403. The integration user lacks a required role "
                "(itil for incidents, knowledge for KB, cmdb_read for the CMDB)."
            )
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("error", {}).get("message", "")
            except Exception:  # pragma: no cover - non-JSON error body
                detail = response.text[:300]
            raise BackendError(f"ServiceNow returned {response.status_code}: {detail}")
        payload = response.json()
        return payload.get("result", payload)

    def _table_get(self, table: str, query: str, fields: str, limit: int) -> list[dict[str, Any]]:
        params = {
            "sysparm_query": query,
            "sysparm_fields": fields,
            "sysparm_limit": str(max(1, min(limit, 100))),
            "sysparm_display_value": "all",
            "sysparm_exclude_reference_link": "true",
        }
        result = self._request("GET", f"/api/now/table/{table}", params=params)
        return [_flatten_record(row) for row in (result or [])]

    def _table_one(self, table: str, query: str, fields: str) -> dict[str, Any] | None:
        rows = self._table_get(table, query, fields, 1)
        return rows[0] if rows else None

    def _table_post(self, table: str, body: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"/api/now/table/{table}",
            params={"sysparm_display_value": "all", "sysparm_exclude_reference_link": "true"},
            json=body,
        )
        return _flatten_record(result or {})

    def _table_patch(self, table: str, sys_id: str, body: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "PATCH",
            f"/api/now/table/{table}/{sys_id}",
            params={"sysparm_display_value": "all", "sysparm_exclude_reference_link": "true"},
            json=body,
        )
        return _flatten_record(result or {})

    # -------------------------------------------------------------- incidents

    def _incident_sys_id(self, number: str) -> dict[str, Any]:
        record = self._table_one("incident", f"number={number}", INCIDENT_FIELDS)
        if not record:
            raise BackendError(
                f"No incident matches {number!r}. Use search_incidents rather than guessing a number."
            )
        return record

    def create_incident(self, **fields: Any) -> dict[str, Any]:
        self._guard_write()
        body = {key: value for key, value in fields.items() if value not in (None, "")}
        if "state" in body:
            body["state"] = normalise_state(body["state"]) or "1"
        body.pop("priority", None)  # derived from impact x urgency by the platform
        created = self._table_post("incident", body)
        return shape_incident(created)

    def get_incident(self, number: str) -> dict[str, Any]:
        record = self._incident_sys_id(number)
        shaped = shape_incident(record)
        shaped["work_notes"] = self._journal(record.get("sys_id", ""), "work_notes")
        shaped["comments"] = self._journal(record.get("sys_id", ""), "comments")
        return shaped

    def _journal(self, sys_id: str, field: str) -> list[dict[str, Any]]:
        if not sys_id:
            return []
        rows = self._table_get(
            "sys_journal_field",
            f"element_id={sys_id}^element={field}^ORDERBYsys_created_on",
            "value,sys_created_on,sys_created_by",
            50,
        )
        return [
            {
                "created_on": row.get("sys_created_on", ""),
                "created_by": row.get("sys_created_by", ""),
                "value": row.get("value", ""),
            }
            for row in rows
        ]

    def search_incidents(self, encoded_query: str, limit: int, order_by: str | None = None) -> list[dict[str, Any]]:
        query = encoded_query or ""
        if order_by:
            keyword = "ORDERBYDESC" if order_by.startswith("-") else "ORDERBY"
            query = f"{query}^{keyword}{order_by.lstrip('-')}" if query else f"{keyword}{order_by.lstrip('-')}"
        return [summarise_incident(row) for row in self._table_get("incident", query, INCIDENT_FIELDS, limit)]

    def update_incident(self, number: str, patch: dict[str, Any], work_note: str | None = None) -> dict[str, Any]:
        self._guard_write()
        record = self._incident_sys_id(number)
        body = {key: value for key, value in patch.items() if value not in (None, "")}
        if "state" in body:
            body["state"] = normalise_state(body["state"]) or body["state"]
        body.pop("priority", None)
        if work_note:
            body["work_notes"] = work_note
        if not body:
            raise BackendError("update_incident needs at least one field to change or a work_note to add")
        updated = self._table_patch("incident", record["sys_id"], body)
        return shape_incident(updated)

    def add_comment(self, number: str, comment: str) -> dict[str, Any]:
        self._guard_write()
        record = self._incident_sys_id(number)
        updated = self._table_patch("incident", record["sys_id"], {"comments": comment})
        return shape_incident(updated)

    def resolve_incident(self, number: str, close_code: str, close_notes: str) -> dict[str, Any]:
        self._guard_write()
        if close_code not in CLOSE_CODES:
            raise BackendError(f"close_code must be one of {CLOSE_CODES}")
        record = self._incident_sys_id(number)
        updated = self._table_patch(
            "incident",
            record["sys_id"],
            {"state": "6", "close_code": close_code, "close_notes": close_notes},
        )
        return shape_incident(updated)

    def similar_incidents(self, text: str, cmdb_ci: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """ServiceNow has no similarity endpoint on the Table API, so score client-side."""
        from ..store import tokenize

        query = "state IN 6,7" if not cmdb_ci else f"cmdb_ci.name={cmdb_ci}"
        candidates = self._table_get("incident", f"{query}^ORDERBYDESCopened_at", INCIDENT_FIELDS, 60)
        needle = tokenize(text)
        scored = []
        for row in candidates:
            haystack = tokenize(
                f"{row.get('short_description','')} {row.get('description','')} {row.get('close_notes','')}"
            )
            overlap = len(needle & haystack)
            if not overlap:
                continue
            score = overlap / max(len(needle | haystack), 1)
            scored.append((score, row))
        scored.sort(key=lambda item: -item[0])
        return [
            {
                "number": row.get("number", ""),
                "short_description": row.get("short_description", ""),
                "state": STATE_LABELS.get(str(row.get("state", "")), row.get("state", "")),
                "cmdb_ci": row.get("cmdb_ci", ""),
                "close_code": row.get("close_code", ""),
                "close_notes": row.get("close_notes", ""),
                "opened_at": row.get("opened_at", ""),
                "similarity": round(score, 3),
            }
            for score, row in scored[:limit]
        ]

    def aggregate_incidents(self, group_by: str, encoded_query: str = "") -> list[dict[str, Any]]:
        params = {
            "sysparm_query": encoded_query or "",
            "sysparm_group_by": group_by,
            "sysparm_count": "true",
        }
        result = self._request("GET", f"/api/now/stats/{'incident'}", params=params)
        rows = result if isinstance(result, list) else (result or {}).get("result", [])
        buckets = []
        for row in rows:
            groupby = {item["field"]: item["value"] for item in row.get("groupby_fields", [])}
            name = groupby.get(group_by, "(empty)")
            if group_by == "state":
                name = STATE_LABELS.get(str(name), str(name))
            elif group_by == "priority":
                name = PRIORITY_LABELS.get(str(name), str(name))
            buckets.append({"group": name, "count": int(row.get("stats", {}).get("count", 0))})
        buckets.sort(key=lambda item: -item["count"])
        return buckets

    # -------------------------------------------------------------- knowledge

    def search_knowledge(self, text: str, category: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        query = f"workflow_state=published^123TEXTQUERY321={text}"
        if category:
            query += f"^kb_category.labelLIKE{category}"
        rows = self._table_get("kb_knowledge", query, KB_FIELDS, limit)
        return [
            {
                "number": row.get("number", ""),
                "short_description": row.get("short_description", ""),
                "category": row.get("kb_category", ""),
                "relevance": round(1.0 - index * 0.1, 3),
                "snippet": (row.get("text", "") or "")[:280],
            }
            for index, row in enumerate(rows)
        ]

    def get_article(self, number: str) -> dict[str, Any]:
        row = self._table_one("kb_knowledge", f"number={number}", KB_FIELDS)
        if not row:
            raise BackendError(f"No knowledge article matches {number!r}. Use search_knowledge first.")
        return {
            "number": row.get("number", ""),
            "short_description": row.get("short_description", ""),
            "category": row.get("kb_category", ""),
            "keywords": row.get("keywords", ""),
            "workflow_state": row.get("workflow_state", ""),
            "view_count": row.get("sys_view_count", ""),
            "text": row.get("text", ""),
        }

    # ------------------------------------------------------------------- cmdb

    def search_cmdb(self, encoded_query: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._table_get("cmdb_ci", encoded_query or "", CI_FIELDS, limit)
        return [self._shape_ci(row) for row in rows]

    def get_ci(self, name: str) -> dict[str, Any]:
        row = self._table_one("cmdb_ci", f"name={name}", CI_FIELDS)
        if not row:
            raise BackendError(f"No configuration item matches {name!r}. Use search_cmdb first.")
        shaped = self._shape_ci(row)
        open_incidents = self._table_get(
            "incident", f"cmdb_ci.name={name}^active=true", INCIDENT_FIELDS, 10
        )
        shaped["open_incidents"] = [summarise_incident(item) for item in open_incidents]
        shaped["open_incident_count"] = len(open_incidents)
        return shaped

    def ci_relationships(self, name: str, direction: str = "both", depth: int = 2) -> dict[str, Any]:
        row = self._table_one("cmdb_ci", f"name={name}", "name,sys_id")
        if not row:
            raise BackendError(f"No configuration item matches {name!r}.")
        depth = max(1, min(int(depth), 3))
        upstream = self._walk(row["name"], "parent", depth) if direction in ("both", "upstream") else []
        downstream = self._walk(row["name"], "child", depth) if direction in ("both", "downstream") else []
        return {
            "ci": row["name"],
            "depth": depth,
            "upstream": upstream,
            "downstream": downstream,
            "legend": {
                "upstream": "Configuration items this CI depends on.",
                "downstream": "Configuration items impacted if this CI fails.",
            },
        }

    def _walk(self, name: str, side: str, depth: int) -> list[dict[str, Any]]:
        other = "child" if side == "parent" else "parent"
        found: dict[str, int] = {}
        frontier = [(name, 0)]
        while frontier:
            current, level = frontier.pop(0)
            if level >= depth:
                continue
            rows = self._table_get(
                "cmdb_rel_ci", f"{side}.name={current}", "parent,child,type", 50
            )
            for rel in rows:
                target = rel.get(other, "")
                if not target or target in found:
                    continue
                found[target] = level + 1
                frontier.append((target, level + 1))
        return [{"name": key, "hops": value} for key, value in sorted(found.items(), key=lambda i: i[1])]

    @staticmethod
    def _shape_ci(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": record.get("name", ""),
            "sys_class_name": record.get("sys_class_name", ""),
            "short_description": record.get("short_description", ""),
            "operational_status": record.get("operational_status", ""),
            "environment": record.get("environment", ""),
            "support_group": record.get("support_group", ""),
            "business_criticality": record.get("business_criticality", ""),
            "owned_by": record.get("owned_by", ""),
            "location": record.get("location", ""),
            "ip_address": record.get("ip_address", ""),
            "sys_id": record.get("sys_id", ""),
        }

    # ----------------------------------------------------------------- people

    def lookup_user(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        encoded = (
            f"active=true^nameLIKE{query}^ORuser_nameLIKE{query}"
            f"^ORemailLIKE{query}^ORdepartmentLIKE{query}"
        )
        rows = self._table_get("sys_user", encoded, USER_FIELDS, limit)
        return [
            {
                "name": row.get("name", ""),
                "user_name": row.get("user_name", ""),
                "email": row.get("email", ""),
                "department": row.get("department", ""),
                "title": row.get("title", ""),
                "vip": row.get("vip", "false"),
                "manager": row.get("manager", ""),
                "location": row.get("location", ""),
            }
            for row in rows
        ]

    def close(self) -> None:
        self._client.close()
