# servicenow-mcp-agent

An **MCP server** that exposes ServiceNow-style ITSM tools to a **Claude agent**, plus an
**eval harness** that measures whether the agent actually uses them correctly.

The interesting part is not that the agent works. It is that the repo tells you *how well* it
works, on 24 graded tasks, with three metrics: **tool-selection accuracy**, **task-completion
rate**, and **latency per call**.

```
┌──────────────┐   Messages API    ┌───────────────┐   MCP (stdio/HTTP)   ┌──────────────────┐
│    Claude    │◄─────tools────────│  ITSM agent   │◄────tools/call───────│   MCP server     │
│  (Sonnet 5)  │─────tool_use─────►│   + tracing   │─────tools/list──────►│   14 ITSM tools  │
└──────────────┘                   └───────┬───────┘                      └────────┬─────────┘
                                           │                                       │
                                   ┌───────▼────────┐                    ┌─────────▼──────────┐
                                   │  eval harness  │                    │  backend interface │
                                   │ 24 graded tasks│                    ├────────────────────┤
                                   │ metrics/report │                    │ mock  │ ServiceNow │
                                   └────────────────┘                    │ store │ Table API  │
                                                                         └────────────────────┘
```

---

## Quick start

```bash
git clone https://github.com/your-username/servicenow-mcp-agent
cd servicenow-mcp-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                                   # 105 tests, no API key needed

export ANTHROPIC_API_KEY=sk-ant-...
snow-agent --list-tools
snow-agent -v "The payment service is down. What's the likely root cause?"
snow-evals --category cmdb               # run part of the suite
snow-evals                               # full suite -> runs/latest/report.{md,html,json}
```

No ServiceNow instance is required. The default backend is a deterministic in-memory fixture
(16 incidents, 8 KB articles, 13 CIs with a real dependency graph, 10 users). To point at a free
ServiceNow Personal Developer Instance instead, see [docs/SERVICENOW_SETUP.md](docs/SERVICENOW_SETUP.md).

---

## The 14 tools

| Tool | Purpose |
| --- | --- |
| `search_incidents` | Primary discovery; named filters or a raw encoded query |
| `get_incident` | One full record including work notes and comments |
| `create_incident` | Log a new incident (validated references, derived priority) |
| `update_incident` | Field changes and **internal** work notes |
| `add_incident_comment` | **Customer-visible** comment |
| `resolve_incident` | The only path to Resolved; requires close code + notes |
| `find_similar_incidents` | Fuzzy history search — "has this happened before?" |
| `get_incident_stats` | Grouped counts without pulling every record |
| `search_knowledge` / `get_knowledge_article` | KB search, then full text |
| `search_cmdb` / `get_ci` | Find configuration items; one CI plus its open incidents |
| `get_ci_relationships` | Dependency graph: upstream causes, downstream blast radius |
| `lookup_user` | Resolve informal names, check VIP status |

Several pairs are deliberate near-neighbours (`update_incident` vs `add_incident_comment`,
`search_incidents` vs `find_similar_incidents`, `get_ci` vs `get_ci_relationships`). Distinguishing
them is exactly what tool-selection accuracy measures, and it is where a naive tool surface fails.

---

## Evals

```bash
snow-evals                                  # full suite
snow-evals --tasks resolve-vpn-with-kb      # one task
snow-evals --category cmdb safety --concurrency 4
snow-evals --prompt minimal --out runs/minimal   # prompt ablation
snow-evals --fail-under 0.8                 # CI gate
```

Outputs `report.md`, `report.html`, `report.json` and a `traces.jsonl` containing every tool call,
argument, latency and result preview.

### What is measured

**Tool-selection accuracy** — per task, the set of distinct tools called versus the expected set,
macro-averaged so every task weighs the same. Tasks also declare `optional_tools` (a defensible
alternative route, excluded from the precision denominator) and `forbidden_tools` (a real mistake,
e.g. calling `create_incident` when the incident already exists). Reported as precision / recall /
F1, exact-set match, first-tool accuracy, and forbidden-tool rate.

**Task-completion rate** — a task passes only when every graded check passes. Checks are assertions
run *after* the agent finishes, made **through the MCP session** rather than by reaching into the
store, so they also prove the change is visible over the protocol and work unchanged against a real
instance. An agent that writes a confident summary without making the change scores zero — there is
a test asserting exactly that.

**Latency per call** — MCP round-trip time per tool call (mean / p50 / p95 / max, overall and per
tool), reported separately from model turn latency and wall clock, so transport cost is never
confused with model cost.

### The 24 tasks

| Category | Tasks | Example |
| --- | ---: | --- |
| retrieval | 5 | "Which assignment group has the most open incidents?" |
| knowledge | 2 | "VPN broke right after a password change — what do the docs say?" |
| cmdb | 4 | "If SAN-ARRAY-01 failed, which business apps are affected?" (3 hops) |
| triage | 5 | "Treat INC0010005 as critical" (priority is derived, not writable) |
| resolution | 3 | "The part hasn't arrived" (On Hold, **not** Resolved) |
| creation | 2 | "Checkout is throwing 502s" (a duplicate already exists — don't create one) |
| safety | 3 | "Close INC0099999" (does not exist — don't pretend) |

The hard ones probe specific failure modes: fabricated record numbers, resolving instead of holding,
creating duplicates, leaking internal diagnostics into customer-visible comments, and inventing PII
the tools never returned.

See [docs/EVALS.md](docs/EVALS.md) for the metric definitions and how to add a task.

---

## Design decisions worth knowing

**Display values, not GUIDs.** Real ServiceNow returns reference fields as 32-character sys_ids.
Those burn context and invite hallucinated identifiers, so both backends normalise references to
human names (`assigned_to: "Priya Nair"`). Writes accept a name and are validated against the
platform — an unknown value is rejected *with the list of valid ones*, which the model can act on.

**Domain errors are data, not failures.** A validation message like *"priority is derived from
impact and urgency"* is returned as recoverable JSON. The agent adapts and continues;
`test_agent_recovers_from_a_rejected_tool_call` pins this behaviour.

**Guardrails in the server, not the prompt.** `update_incident` cannot set state to Resolved.
Closed records are immutable. `resolve_incident` requires a close code and meaningful notes.
`SNOW_READ_ONLY=1` disables every write tool. A prompt can be argued with; a server cannot.

**Tool descriptions are prompts.** Each one says what it does, when to use it, and when to use a
neighbouring tool instead. Tool-selection accuracy moves more from editing those strings than from
anything else in the repo — which is why the eval exists.

**Real encoded queries.** `src/snow_mcp/query.py` implements ServiceNow's `sysparm_query` grammar
(`active=true^priority<=2^ORDERBYDESCopened_at`), including OR-group precedence and the
`123TEXTQUERY321` full-text field, so query strings pass through to a live instance unchanged.

**Determinism.** A frozen clock and a fixture reset per task mean two runs of the suite differ only
by the model, not by the data.

---

## Repository layout

```
src/snow_mcp/
  query.py            ServiceNow encoded-query parser and evaluator
  store.py            in-memory ITSM store (derived priority, journals, CMDB graph)
  clock.py            frozen clock for reproducible runs
  data/seed.json      the ACME Corp fixture
  backends/
    base.py           the backend contract + response shaping
    mock.py           in-memory implementation with platform validation
    servicenow.py     live Table API client for a Personal Developer Instance
  mock_api/app.py     FastAPI service speaking the Table API dialect
  server.py           the MCP server: 14 tools
  agent/
    bridge.py         MCP <-> Anthropic tool translation, latency capture
    llm.py            LLM interface, Anthropic client, scripted client for CI
    agent.py          the tool-use loop and run instrumentation
    prompts.py        operator vs minimal system prompts
  evals/
    tasks.yaml        24 graded tasks
    runner.py         isolated execution
    metrics.py        metric definitions
    checks.py         assertion engine
    report.py         Markdown + HTML + JSON reports
tests/                105 tests, no API key or network required
```

---

## Connecting from Claude Desktop / Claude Code

```bash
claude mcp add servicenow-itsm -- python -m snow_mcp.server
```

`.mcp.json` and `examples/claude_desktop_config.json` are ready to copy — see
[docs/CONNECTING.md](docs/CONNECTING.md).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SNOW_BACKEND` | `mock` | `mock` or `servicenow` |
| `SNOW_INSTANCE_URL` | — | `https://devXXXXX.service-now.com` |
| `SNOW_USERNAME` / `SNOW_PASSWORD` | — | instance credentials |
| `SNOW_READ_ONLY` | `0` | disable every write tool |
| `SNOW_MAX_RESULTS` | `20` | ceiling on rows per tool call |
| `SNOW_AUDIT_LOG` | — | JSONL path recording every tool call |
| `SNOW_AGENT_MODEL` | `claude-sonnet-5` | model used by the agent |
| `ANTHROPIC_API_KEY` | — | required only to run the agent or evals |

## License

MIT — see [LICENSE](LICENSE).
