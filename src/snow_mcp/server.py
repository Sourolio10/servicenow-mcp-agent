"""MCP server exposing ServiceNow-style ITSM operations as tools.

Every tool docstring is a prompt. The model never sees this source file, only
the name, the description and the JSON schema, so each description states what
the tool does, when to reach for it, and — critically — when to reach for a
neighbouring tool instead. Tool-selection accuracy in the eval suite moves more
from editing these strings than from anything else in the repo.

Run it::

    snow-mcp-server                      # stdio, for Claude Desktop / Claude Code
    snow-mcp-server --transport streamable-http --port 8000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer

from .backends.base import BackendError
from .config import Settings, build_backend
from .store import CLOSE_CODES, ValidationError

logger = logging.getLogger("snow_mcp.server")

SERVER_INSTRUCTIONS = """\
ServiceNow-style ITSM tools for incident management, knowledge search and CMDB lookup.

Conventions used by every tool in this server:
* Incidents are referenced by number (INC0010001), never by sys_id.
* state codes: 1=New, 2=In Progress, 3=On Hold, 6=Resolved, 7=Closed, 8=Canceled.
* impact and urgency are 1 (high), 2 (medium), 3 (low). Priority is DERIVED from
  the two and cannot be written directly.
* Reference fields (caller_id, assigned_to, assignment_group, cmdb_ci) take
  human-readable names and are validated against the platform; an unknown value
  is rejected rather than silently created.
* Read before you write. Never pass an incident number, CI name or user name
  that did not come back from a search tool in this session.
"""


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def _audit(settings: Settings, tool: str, arguments: dict[str, Any], outcome: str) -> None:
    if not settings.audit_log:
        return
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": settings.actor,
        "tool": tool,
        "arguments": arguments,
        "outcome": outcome,
    }
    try:
        with open(settings.audit_log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
    except OSError:  # pragma: no cover - auditing must never break a tool call
        logger.warning("could not write audit log to %s", settings.audit_log)


def _guarded(settings: Settings, name: str, fn: Callable[..., Any], arguments: dict[str, Any]) -> str:
    """Run a backend call, converting domain errors into agent-readable text.

    Domain errors are returned as data rather than raised. A validation message
    such as "priority is derived from impact and urgency" is a recoverable
    instruction the model can act on; a protocol-level error just ends the turn.
    """
    try:
        result = fn()
    except (BackendError, ValidationError) as exc:
        _audit(settings, name, arguments, f"error: {exc}")
        return _json({"error": str(exc), "tool": name, "recoverable": True})
    except Exception as exc:  # pragma: no cover - unexpected, still shouldn't crash the server
        logger.exception("unhandled error in %s", name)
        _audit(settings, name, arguments, f"unhandled: {exc}")
        return _json({"error": f"Unexpected {type(exc).__name__}: {exc}", "tool": name, "recoverable": False})
    _audit(settings, name, arguments, "ok")
    return _json(result)


def _bounded(value: int | None, default: int, ceiling: int) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(1, min(number, ceiling))


def build_server(backend: Any = None, settings: Settings | None = None) -> MCPServer:
    """Construct the MCP server. Exposed as a factory so tests can drive it in-memory."""
    settings = settings or Settings.from_env()
    backend = backend or build_backend(settings)
    ceiling = settings.max_results

    server = MCPServer(
        name="servicenow-itsm",
        title="ServiceNow ITSM",
        version="0.1.0",
        instructions=SERVER_INSTRUCTIONS,
    )

    # ------------------------------------------------------------ incidents

    @server.tool()
    def search_incidents(
        text: str | None = None,
        state: str | None = None,
        priority: str | None = None,
        caller: str | None = None,
        assigned_to: str | None = None,
        assignment_group: str | None = None,
        cmdb_ci: str | None = None,
        active_only: bool = True,
        opened_after: str | None = None,
        encoded_query: str | None = None,
        order_by: str | None = "-opened_at",
        limit: int = 10,
    ) -> str:
        """Find incidents matching a filter. This is the primary discovery tool.

        Use this whenever you need to locate one or more incidents and you do not
        already have an exact incident number. Returns a compact summary of each
        match, not the full record.

        Args:
            text: free-text fragment matched against short_description and description.
            state: New, In Progress, On Hold, Resolved, Closed, or the numeric code.
            priority: 1-5, where 1 is critical. Accepts "1" or "<=2" style comparisons.
            caller: name of the person who reported the incident.
            assigned_to: name of the engineer the incident is assigned to.
            assignment_group: e.g. "Network Operations", "Database Administration".
            cmdb_ci: exact configuration item name, e.g. "PAY-APP-01".
            active_only: when true (default) exclude Resolved and Closed incidents.
                Set false when looking for historical or previously solved incidents.
            opened_after: ISO timestamp, e.g. "2026-08-22 00:00:00".
            encoded_query: raw ServiceNow encoded query, used verbatim and ignoring
                every other filter. Escape hatch for conditions the named
                arguments cannot express.
            order_by: field name; prefix with "-" for descending. Default newest first.
            limit: maximum rows to return (1-{ceiling}).

        Prefer get_incident when you already know the exact number.
        Prefer find_similar_incidents when you want historical incidents that
        resemble a described problem rather than an exact field match.
        Prefer get_incident_stats when you only need counts per group.
        """
        limit = _bounded(limit, 10, ceiling)
        if encoded_query:
            query = encoded_query
        else:
            clauses: list[str] = []
            if text:
                clauses.append(f"short_descriptionLIKE{text}^ORdescriptionLIKE{text}")
            if state:
                from .store import normalise_state

                code = normalise_state(state)
                clauses.append(f"state={code}" if code else f"state={state}")
            if priority:
                stripped = str(priority).strip()
                if stripped[:1] in ("<", ">", "!"):
                    operator = stripped[:2] if stripped[1:2] == "=" else stripped[:1]
                    clauses.append(f"priority{operator}{stripped[len(operator):]}")
                else:
                    clauses.append(f"priority={stripped}")
            if caller:
                clauses.append(f"caller_idLIKE{caller}")
            if assigned_to:
                clauses.append(f"assigned_toLIKE{assigned_to}")
            if assignment_group:
                clauses.append(f"assignment_groupLIKE{assignment_group}")
            if cmdb_ci:
                clauses.append(f"cmdb_ci={cmdb_ci}")
            if opened_after:
                clauses.append(f"opened_at>{opened_after}")
            if active_only:
                clauses.append("active=true")
            query = "^".join(clauses)

        def run() -> dict[str, Any]:
            rows = backend.search_incidents(query, limit, order_by)
            return {"query": query, "count": len(rows), "incidents": rows}

        return _guarded(settings, "search_incidents", run, {"query": query, "limit": limit})

    @server.tool()
    def get_incident(number: str) -> str:
        """Retrieve the complete record for one incident, including its work notes and comments.

        Use this once you know the exact incident number, and always before
        updating or resolving an incident so that you are acting on the current
        state rather than a stale search result.

        Args:
            number: incident number such as "INC0010001".

        Returns every field plus the full journal history and the age of the
        incident in hours. Use search_incidents if you do not have a number.
        """
        return _guarded(settings, "get_incident", lambda: backend.get_incident(number), {"number": number})

    @server.tool()
    def create_incident(
        short_description: str,
        description: str | None = None,
        caller: str | None = None,
        category: str | None = None,
        impact: str = "3",
        urgency: str = "3",
        cmdb_ci: str | None = None,
        assignment_group: str | None = None,
    ) -> str:
        """Log a NEW incident. Creates a permanent record; do not call speculatively.

        Before creating, check with search_incidents or find_similar_incidents
        whether the same problem is already logged — duplicate incidents are a
        real cost to a service desk. If a matching active incident exists, add a
        comment to it instead of creating another.

        Args:
            short_description: one-line summary. Required.
            description: fuller detail including symptoms, timing and scope.
            caller: name of the person reporting it; must be a known user.
            category: one of hardware, software, network, database, inquiry.
            impact: 1 high (whole site/service), 2 medium (a department),
                3 low (one person). Default 3.
            urgency: 1 high, 2 medium, 3 low. Default 3.
            cmdb_ci: the affected configuration item name if known.
            assignment_group: routing group; defaults to Service Desk.

        Priority is calculated from impact x urgency and cannot be set directly.
        """
        fields = {
            "short_description": short_description,
            "description": description,
            "caller_id": caller,
            "category": category,
            "impact": impact,
            "urgency": urgency,
            "cmdb_ci": cmdb_ci,
            "assignment_group": assignment_group,
        }
        return _guarded(
            settings, "create_incident", lambda: backend.create_incident(**fields), fields
        )

    @server.tool()
    def update_incident(
        number: str,
        state: str | None = None,
        assigned_to: str | None = None,
        assignment_group: str | None = None,
        impact: str | None = None,
        urgency: str | None = None,
        category: str | None = None,
        cmdb_ci: str | None = None,
        short_description: str | None = None,
        work_note: str | None = None,
    ) -> str:
        """Modify an existing incident's fields and/or add an INTERNAL work note.

        Work notes are visible to IT staff only. Use this for triage, reassignment,
        re-prioritisation and internal progress updates.

        Args:
            number: incident number to update.
            state: New, In Progress, On Hold. Do NOT use this to resolve or close.
            assigned_to: engineer name; must be a known user.
            assignment_group: group name; must be a known group.
            impact / urgency: 1, 2 or 3. Changing either recalculates priority.
            category, cmdb_ci, short_description: corrected field values.
            work_note: internal note appended to the work notes journal.

        Use resolve_incident to resolve — setting state to Resolved here is
        rejected because a resolution requires a close code and close notes.
        Use add_incident_comment when the text should be visible to the caller.
        """
        patch = {
            "state": state,
            "assigned_to": assigned_to,
            "assignment_group": assignment_group,
            "impact": impact,
            "urgency": urgency,
            "category": category,
            "cmdb_ci": cmdb_ci,
            "short_description": short_description,
        }
        patch = {key: value for key, value in patch.items() if value not in (None, "")}
        if patch.get("state") in ("6", "7", "Resolved", "Closed", "resolved", "closed"):
            return _json({
                "error": "Use resolve_incident to move an incident to Resolved; it requires a "
                         "close_code and close_notes. update_incident cannot set state to Resolved or Closed.",
                "tool": "update_incident",
                "recoverable": True,
            })
        return _guarded(
            settings,
            "update_incident",
            lambda: backend.update_incident(number, patch, work_note),
            {"number": number, "patch": patch, "work_note": work_note},
        )

    @server.tool()
    def add_incident_comment(number: str, comment: str) -> str:
        """Add a CUSTOMER-VISIBLE comment to an incident. The caller receives this text.

        Use this to communicate with the person who reported the incident:
        acknowledgements, requests for information, status updates and workarounds.

        Args:
            number: incident number.
            comment: the message the caller will read. Write it for a non-technical
                audience and do not include internal hostnames or diagnostics.

        Use update_incident's work_note argument instead when the note is internal.
        """
        return _guarded(
            settings,
            "add_incident_comment",
            lambda: backend.add_comment(number, comment),
            {"number": number, "comment": comment},
        )

    @server.tool()
    def resolve_incident(number: str, close_code: str, close_notes: str) -> str:
        """Resolve an incident. This is the only correct way to move one to Resolved.

        Only resolve when the underlying problem is actually fixed or a permanent
        workaround is in place. If work is merely paused, use update_incident with
        state "On Hold" instead.

        Args:
            number: incident number.
            close_code: one of {close_codes}.
            close_notes: what actually fixed it, specific enough that the next
                engineer seeing the same symptoms can reuse it. Minimum 10 characters.
        """
        return _guarded(
            settings,
            "resolve_incident",
            lambda: backend.resolve_incident(number, close_code, close_notes),
            {"number": number, "close_code": close_code},
        )

    @server.tool()
    def find_similar_incidents(
        problem_description: str, cmdb_ci: str | None = None, limit: int = 5
    ) -> str:
        """Find PAST incidents resembling a described problem, ranked by similarity.

        This is the tool for "has this happened before?" and for finding the fix
        that worked last time. Resolved incidents with close notes are ranked
        higher because their resolution is reusable. Also use it to detect a
        duplicate before calling create_incident.

        Args:
            problem_description: the symptoms in natural language.
            cmdb_ci: narrow to a configuration item and boost its incidents.
            limit: maximum matches (1-{ceiling}).

        Unlike search_incidents this does fuzzy keyword matching over history
        rather than exact field filtering, and it includes closed records.
        """
        return _guarded(
            settings,
            "find_similar_incidents",
            lambda: {
                "matches": backend.similar_incidents(
                    problem_description, cmdb_ci, _bounded(limit, 5, ceiling)
                )
            },
            {"problem_description": problem_description, "cmdb_ci": cmdb_ci},
        )

    @server.tool()
    def get_incident_stats(group_by: str = "assignment_group", encoded_query: str = "active=true") -> str:
        """Count incidents grouped by a field. Use for "how many", "which group has most", reporting.

        Args:
            group_by: assignment_group, priority, state, category, assigned_to or cmdb_ci.
            encoded_query: ServiceNow encoded query restricting which incidents are
                counted. Defaults to active incidents only. Examples:
                "active=true^priority<=2", "opened_at>2026-08-22 00:00:00".

        Returns counts per group, largest first. Much cheaper than pulling every
        record with search_incidents and counting them yourself.
        """
        return _guarded(
            settings,
            "get_incident_stats",
            lambda: {
                "group_by": group_by,
                "query": encoded_query,
                "buckets": backend.aggregate_incidents(group_by, encoded_query),
            },
            {"group_by": group_by, "encoded_query": encoded_query},
        )

    # ------------------------------------------------------------ knowledge

    @server.tool()
    def search_knowledge(text: str, category: str | None = None, limit: int = 5) -> str:
        """Search the knowledge base for articles about a problem or procedure.

        Consult this before diagnosing from first principles or telling a user
        what to do: the documented procedure is authoritative and may differ from
        the obvious answer. Returns titles and short snippets only.

        Args:
            text: symptoms, error text or the procedure you need.
            category: optional filter, e.g. Network, Applications, Hardware,
                Database, Process, "Accounts and Access".
            limit: maximum articles (1-{ceiling}).

        Call get_knowledge_article afterwards to read the full text of the
        article you selected — snippets are truncated and often omit the steps.
        """
        return _guarded(
            settings,
            "search_knowledge",
            lambda: {"articles": backend.search_knowledge(text, category, _bounded(limit, 5, ceiling))},
            {"text": text, "category": category},
        )

    @server.tool()
    def get_knowledge_article(number: str) -> str:
        """Read the full text of one knowledge article.

        Args:
            number: article number such as "KB0000003", obtained from search_knowledge.

        Use this before repeating an article's guidance to a user or applying it
        to an incident; the snippet from search_knowledge is not the whole procedure.
        """
        return _guarded(
            settings, "get_knowledge_article", lambda: backend.get_article(number), {"number": number}
        )

    # ----------------------------------------------------------------- cmdb

    @server.tool()
    def search_cmdb(
        name: str | None = None,
        ci_class: str | None = None,
        environment: str | None = None,
        support_group: str | None = None,
        encoded_query: str | None = None,
        limit: int = 10,
    ) -> str:
        """Search the CMDB for configuration items (servers, applications, databases, network devices).

        Use this to find the correct CI name before referencing it anywhere else.
        CI names are exact strings such as "PAY-APP-01"; do not invent them.

        Args:
            name: full or partial CI name.
            ci_class: e.g. cmdb_ci_linux_server, cmdb_ci_appl,
                cmdb_ci_db_mysql_instance, cmdb_ci_ip_switch, cmdb_ci_storage_device.
            environment: production, staging, development.
            support_group: the group that owns the CI.
            encoded_query: raw encoded query, overrides the other filters.
            limit: maximum rows (1-{ceiling}).

        Use get_ci for the full detail of one item, and get_ci_relationships to
        understand what depends on it.
        """
        limit = _bounded(limit, 10, ceiling)
        if encoded_query:
            query = encoded_query
        else:
            clauses = []
            if name:
                clauses.append(f"nameLIKE{name}^ORshort_descriptionLIKE{name}")
            if ci_class:
                clauses.append(f"sys_class_nameLIKE{ci_class}")
            if environment:
                clauses.append(f"environment={environment}")
            if support_group:
                clauses.append(f"support_groupLIKE{support_group}")
            query = "^".join(clauses)
        return _guarded(
            settings,
            "search_cmdb",
            lambda: {"query": query, "items": backend.search_cmdb(query, limit)},
            {"query": query},
        )

    @server.tool()
    def get_ci(name: str) -> str:
        """Get one configuration item in full, together with its open incidents.

        Args:
            name: exact CI name such as "PAY-DB-01".

        Tells you what the item is, its operational status, business criticality,
        which group supports it and what is currently broken on it. Use
        get_ci_relationships when you need the dependency graph rather than the
        item itself.
        """
        return _guarded(settings, "get_ci", lambda: backend.get_ci(name), {"name": name})

    @server.tool()
    def get_ci_relationships(name: str, direction: str = "both", depth: int = 2) -> str:
        """Traverse CMDB dependencies to answer impact and root-cause questions.

        Args:
            name: exact CI name.
            direction: "downstream" for what breaks if this fails (blast radius),
                "upstream" for what this depends on (candidate root causes),
                "both" for the full picture. Default both.
            depth: relationship hops to follow, 1-5. Default 2. Increase when a
                dependency chain is longer than two links.

        Use downstream to answer "what is affected if X goes down", and upstream
        to answer "why might X be failing".
        """
        return _guarded(
            settings,
            "get_ci_relationships",
            lambda: backend.ci_relationships(name, direction, depth),
            {"name": name, "direction": direction, "depth": depth},
        )

    # --------------------------------------------------------------- people

    @server.tool()
    def lookup_user(query: str, limit: int = 5) -> str:
        """Look up a person: their exact name, email, department, manager and VIP status.

        Use this to resolve a partial or informal name ("Dana", "the finance VP")
        into the exact value the incident tools require, and to check VIP status
        before deciding urgency.

        Args:
            query: name fragment, username, email or department.
            limit: maximum matches (1-{ceiling}).
        """
        return _guarded(
            settings,
            "lookup_user",
            lambda: {"users": backend.lookup_user(query, _bounded(limit, 5, ceiling))},
            {"query": query},
        )

    # Interpolate runtime values into the descriptions the model actually reads.
    _interpolate(server, {"ceiling": ceiling, "close_codes": ", ".join(CLOSE_CODES)})
    return server


def _interpolate(server: MCPServer, values: dict[str, Any]) -> None:
    """Substitute {placeholders} in registered tool descriptions."""
    try:
        tools = server._tool_manager._tools  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - depends on mcp internals
        return
    for tool in tools.values():
        description = getattr(tool, "description", None)
        if not description:
            continue
        for key, value in values.items():
            description = description.replace("{" + key + "}", str(value))
        try:
            tool.description = description
        except Exception:  # pragma: no cover - frozen model
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ServiceNow-style ITSM MCP server")
    parser.add_argument(
        "--transport",
        default=os.environ.get("SNOW_TRANSPORT", "stdio"),
        choices=["stdio", "sse", "streamable-http"],
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--read-only", action="store_true", help="disable every write tool")
    parser.add_argument("--log-level", default=os.environ.get("SNOW_LOG_LEVEL", "WARNING"))
    args = parser.parse_args(argv)

    # stdio transport speaks JSON-RPC on stdout; logs must go to stderr.
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr)

    settings = Settings.from_env()
    if args.read_only:
        settings.read_only = True

    server = build_server(settings=settings)
    logger.info("starting servicenow-itsm MCP server (backend=%s, read_only=%s)",
                settings.backend, settings.read_only)
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.settings.port = args.port  # type: ignore[attr-defined]
        server.run(transport=args.transport)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
