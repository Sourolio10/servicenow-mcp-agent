"""A FastAPI service that speaks the ServiceNow Table API dialect.

This exists so the *live* ServiceNow backend can be exercised end to end without
a Personal Developer Instance:

    snow-mock-api --port 8080
    SNOW_BACKEND=servicenow SNOW_INSTANCE_URL=http://127.0.0.1:8080 \\
        SNOW_USERNAME=admin SNOW_PASSWORD=admin snow-mcp-server

It implements the subset the backend uses: ``GET/POST/PATCH /api/now/table/{table}``
with ``sysparm_query``, ``sysparm_fields``, ``sysparm_limit`` and
``sysparm_display_value``, plus ``GET /api/now/stats/{table}`` for aggregates.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ..store import ITSMStore

MOCK_USERNAME = os.environ.get("SNOW_MOCK_USERNAME", "admin")
MOCK_PASSWORD = os.environ.get("SNOW_MOCK_PASSWORD", "admin")

store = ITSMStore()
app = FastAPI(
    title="Mock ServiceNow Table API",
    version="0.1.0",
    description="ServiceNow-compatible Table API over an in-memory ITSM fixture.",
)


def _check_auth(authorization: str | None) -> None:
    if not authorization or not authorization.lower().startswith("basic "):
        raise HTTPException(status_code=401, detail="User Not Authenticated")
    try:
        decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode()
        username, _, password = decoded.partition(":")
    except Exception:
        raise HTTPException(status_code=401, detail="User Not Authenticated") from None
    if username != MOCK_USERNAME or password != MOCK_PASSWORD:
        raise HTTPException(status_code=401, detail="User Not Authenticated")


def _envelope(value: Any, display: str) -> Any:
    """Reproduce ServiceNow's display_value/value envelope when requested."""
    if display == "all":
        return {"display_value": "" if value is None else str(value), "value": "" if value is None else str(value)}
    return value


# Real ServiceNow uses different column names on some tables than our fixture.
COLUMN_ALIASES = {"kb_category": "category", "sys_view_count": "view_count"}


def _project(record: dict[str, Any], fields: str | None, display: str) -> dict[str, Any]:
    keys = [name.strip() for name in fields.split(",")] if fields else list(record.keys())
    out: dict[str, Any] = {}
    for key in keys:
        value = record.get(key, record.get(COLUMN_ALIASES.get(key, key), ""))
        if isinstance(value, list):
            value = " | ".join(
                entry.get("value", "") if isinstance(entry, dict) else str(entry) for entry in value
            )
        out[key] = _envelope(value, display)
    return out


@app.exception_handler(HTTPException)
async def servicenow_error(request: Request, exc: HTTPException) -> JSONResponse:
    """Match ServiceNow's error body so client error handling is exercised."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.detail, "detail": None}, "status": "failure"},
    )


def _journal_rows() -> list[dict[str, Any]]:
    """Project incident journal entries into ServiceNow's ``sys_journal_field`` shape.

    Real ServiceNow does not store work notes and comments as columns on the
    incident; it writes them to a separate journal table keyed by the record's
    sys_id. The live backend reads them from there, so the mock must present the
    same view.
    """
    rows: list[dict[str, Any]] = []
    for table in ("incident",):
        for record in store.all(table):
            for element in ("work_notes", "comments"):
                for entry in record.get(element, []) or []:
                    rows.append({
                        "sys_id": f"{record['sys_id']}:{element}:{len(rows)}",
                        "element": element,
                        "element_id": record["sys_id"],
                        "name": table,
                        "value": entry.get("value", ""),
                        "sys_created_on": entry.get("created_on", ""),
                        "sys_created_by": entry.get("created_by", ""),
                    })
    return rows


@app.get("/api/now/table/{table}")
async def list_records(
    table: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_auth(authorization)
    params = request.query_params
    limit = int(params.get("sysparm_limit", "20"))
    offset = int(params.get("sysparm_offset", "0"))
    display = params.get("sysparm_display_value", "false")
    query = params.get("sysparm_query", "")
    # The real API dot-walks references; the mock stores display values, so
    # "cmdb_ci.name=X" and "cmdb_ci=X" are equivalent here.
    query = query.replace(".name=", "=").replace(".label", "")
    if table == "sys_journal_field":
        from ..query import apply as apply_query

        rows = apply_query(_journal_rows(), query or None, limit=limit, offset=offset)
    else:
        rows = store.query(table, query or None, limit=limit, offset=offset)
    return {"result": [_project(row, params.get("sysparm_fields"), display) for row in rows]}


@app.get("/api/now/table/{table}/{sys_id}")
async def get_record(
    table: str,
    sys_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_auth(authorization)
    record = store.find(table, sys_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No Record found")
    params = request.query_params
    return {
        "result": _project(
            record, params.get("sysparm_fields"), params.get("sysparm_display_value", "false")
        )
    }


@app.post("/api/now/table/{table}", status_code=201)
async def create_record(
    table: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_auth(authorization)
    body = await request.json()
    if table == "incident" and not body.get("short_description"):
        raise HTTPException(status_code=400, detail="Mandatory field short_description is empty")
    body.pop("priority", None)
    record = store.insert(table, dict(body))
    params = request.query_params
    return {
        "result": _project(
            record, params.get("sysparm_fields"), params.get("sysparm_display_value", "false")
        )
    }


@app.patch("/api/now/table/{table}/{sys_id}")
async def patch_record(
    table: str,
    sys_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_auth(authorization)
    body = await request.json()
    if store.find(table, sys_id) is None:
        raise HTTPException(status_code=404, detail="No Record found")
    journal = {}
    for field in ("work_notes", "comments"):
        if field in body:
            journal[field] = body.pop(field)
    body.pop("priority", None)
    if body:
        record = store.update(table, sys_id, body)
    else:
        record = store.get(table, sys_id)
    for field, value in journal.items():
        record = store.append_journal(table, sys_id, field, value, "mock.api")
    params = request.query_params
    return {
        "result": _project(
            record, params.get("sysparm_fields"), params.get("sysparm_display_value", "false")
        )
    }


@app.get("/api/now/stats/{table}")
async def stats(
    table: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_auth(authorization)
    params = request.query_params
    group_by = params.get("sysparm_group_by", "state")
    query = params.get("sysparm_query", "")
    buckets: dict[str, int] = {}
    for record in store.query(table, query or None):
        key = str(record.get(group_by, "") or "")
        buckets[key] = buckets.get(key, 0) + 1
    return {
        "result": [
            {
                "stats": {"count": str(count)},
                "groupby_fields": [{"field": group_by, "value": name}],
            }
            for name, count in sorted(buckets.items(), key=lambda item: -item[1])
        ]
    }


@app.post("/api/mock/reset")
async def reset(authorization: str | None = Header(default=None)) -> dict[str, str]:
    """Test hook: restore the fixture. Not part of the ServiceNow API."""
    _check_auth(authorization)
    store.reset()
    return {"status": "reset"}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "tables": {name: len(store.tables.get(name, [])) for name in store.tables},
    }


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry point
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Mock ServiceNow Table API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
