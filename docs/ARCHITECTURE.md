# Architecture

## Layers

```
snow_mcp.agent.cli / snow_mcp.evals.cli      entry points
        │
snow_mcp.agent.agent    ITSMAgent            the tool-use loop + instrumentation
        │            ├── snow_mcp.agent.llm      LLM interface (Anthropic | scripted)
        │            └── snow_mcp.agent.bridge   MCP client, schema translation, timing
        │                        │
        │                    MCP protocol (in-process | stdio | streamable-http)
        │                        │
snow_mcp.server         MCPServer            14 tools, argument shaping, error handling
        │
snow_mcp.backends.base  ITSMBackend          the contract
        ├── backends.mock        MockBackend        in-memory, validated
        └── backends.servicenow  ServiceNowBackend  Table API over httpx
                    │
snow_mcp.store          ITSMStore            records, journals, CMDB graph
snow_mcp.query                               encoded-query parser/evaluator
```

Each boundary exists for a reason:

**Backend interface.** The tools never touch a store or an HTTP client. Switching between the
fixture and a live instance is one environment variable, and the eval suite grades both identically.

**LLM interface.** The agent loop depends on `LLMClient`, not the Anthropic SDK. That is what lets
105 tests run in CI with no API key: `ScriptedLLM` replays fixed turns, so loop mechanics (message
threading, `tool_result` plumbing, error recovery, turn limits) are tested independently of model
judgement.

**Bridge.** MCP tool schemas pass through to the Messages API unchanged. That is the point of the
protocol — the tool contract is authored once, on the server, and any MCP client gets it. The bridge
also times every call, which is where the latency metric comes from.

## The tool-use loop

```
user task
   → messages.create(system, messages, tools)
   → stop_reason == "tool_use"?
        yes → for each tool_use block: bridge.call(name, input)
            → append tool_result blocks as a user message
            → repeat
        no  → final answer
```

Bounded by `max_turns` (default 12). Parallel tool calls in a single assistant turn are executed in
order and recorded individually.

Assistant content is stripped of provider-specific keys before being sent back as input — a
round-trip of a raw `model_dump()` is rejected by the API.

## Error handling

Errors are split into two classes:

| Class | Example | Handling |
| --- | --- | --- |
| Domain | unknown caller, priority not writable, incident already resolved | returned as `{"error": ..., "recoverable": true}` JSON with `is_error` set on the tool result; the agent adapts |
| Protocol/unexpected | transport failure, unhandled exception | caught, logged to stderr, returned as non-recoverable; never crashes the server |

Domain errors are written to be *actionable*: an unknown reference value comes back with the list of
valid values, so the model's next call can succeed.

## Determinism

Three sources of nondeterminism are removed so that two suite runs differ only by the model:

1. **Clock** — `snow_mcp.clock.Clock` is frozen at the fixture's anchor instant and advances one
   second per call. `SNOW_LIVE_CLOCK=1` restores wall-clock behaviour.
2. **Identifiers** — `sys_id` is `md5(table:key)`, and incident numbers increment from the fixture's
   high-water mark.
3. **State** — every eval task gets its own `ITSMStore`, `MockBackend` and `MCPServer`. There is a
   test asserting two servers do not share state, because eval isolation depends on it.

## Instrumentation

`AgentRun` records, per task: the ordered tool calls with arguments/latency/outcome, per-turn model
latency and token usage, the stop reason, and the final answer. `to_dict()` serialises the lot into
`traces.jsonl`, which is the raw material for every number in the report.

MCP round-trip latency and model latency are always kept separate. Against the in-memory backend the
former is 1–5 ms; against a real instance it is 200–800 ms. Reporting a single blended figure would
hide which layer to optimise.

## Why the mock is strict

An agent that only ever sees a permissive API learns habits that break in production. The mock
therefore enforces what the platform enforces:

- priority is derived from impact × urgency and rejects direct writes
- reference fields are validated against existing records
- closed incidents are immutable
- resolution requires a valid close code and meaningful close notes
- work notes and comments are separate, append-only journals

Every one of those rules is exercised by a task in the eval suite.
