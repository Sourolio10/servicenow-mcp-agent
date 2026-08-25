# Running against a real ServiceNow instance

The default backend is an in-memory fixture, so nothing here is required. This document is for
pointing the same MCP server at a live instance.

## 1. Get a Personal Developer Instance (free)

1. Sign up at <https://developer.servicenow.com> and select **Request Instance**.
2. Choose the latest release family. You get an instance URL and an `admin` password.
3. PDIs hibernate after a few hours of inactivity and are reclaimed after ~10 days idle — wake it
   from the developer portal before a run.

## 2. Load demo data

A fresh PDI has an empty CMDB. From the instance:

**Studio → Guided Setup**, or load the *ITSM Demo Data* plugin
(**All → System Applications → All Available Applications**, search "Demo Data"). This provides
incidents, knowledge articles, CIs and relationships to work with.

## 3. Create an integration user

Do not run an agent as `admin`. Create a dedicated user with only the roles it needs:

| Capability | Role |
| --- | --- |
| Read/write incidents | `itil` |
| Read knowledge | `knowledge` |
| Read the CMDB | `cmdb_read` (or `asset`) |

Enable **Web service access only** on the user so the credentials cannot be used to log in
interactively.

## 4. Configure

```bash
export SNOW_BACKEND=servicenow
export SNOW_INSTANCE_URL=https://dev123456.service-now.com
export SNOW_USERNAME=mcp.integration
export SNOW_PASSWORD='...'
export SNOW_READ_ONLY=1      # strongly recommended for the first run
```

Then:

```bash
snow-agent --list-tools
snow-agent -v "Show me the open P1 incidents"
```

Start read-only. Once you are satisfied with what the agent chooses to do, drop
`SNOW_READ_ONLY` and set `SNOW_AUDIT_LOG=audit.jsonl` so every call is recorded.

## 5. Test the live client without a PDI

`ServiceNowBackend` is the class that talks to a real instance. The bundled FastAPI app speaks the
same Table API dialect, so the client can be exercised end to end with no instance at all:

```bash
snow-mock-api --port 8080 &
SNOW_BACKEND=servicenow \
SNOW_INSTANCE_URL=http://127.0.0.1:8080 \
SNOW_USERNAME=admin SNOW_PASSWORD=admin \
snow-mcp-server
```

`tests/test_servicenow_backend.py` does this in-process over ASGI — 15 tests covering request
shaping, auth failures, display-value flattening, journal reads and error mapping.

## What differs on a real instance

| Area | Mock | Real |
| --- | --- | --- |
| Reference fields | display values | sys_ids, flattened by the client via `sysparm_display_value=all` |
| Journals | list fields on the incident | rows in `sys_journal_field`, read separately |
| KB category | `category` | `kb_category` |
| Priority | derived by the store | derived by a business rule on the platform |
| Aggregates | computed in memory | `/api/now/stats/{table}` with `sysparm_group_by` |
| Business rules | none | may reject or rewrite writes; check `close_notes` requirements |

The eval suite's `state_checks` run through MCP, so they work against a live instance — but the
seeded task expectations (`INC0010003`, `PAY-DB-01`, ...) refer to the bundled fixture. Write a
separate `tasks.yaml` for instance-specific data.

## Rate limits

PDIs are throttled. Use `--concurrency 1` for eval runs against a real instance, and expect MCP
round-trip latency in the 200–800 ms range rather than the 1–5 ms of the in-memory backend — which
is precisely why the report separates tool latency from model latency.
