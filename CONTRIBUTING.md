# Contributing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check .
```

The test suite runs offline — the LLM is stubbed with `ScriptedLLM` and the MCP server is driven
in-process — so no API key or network access is needed to develop.

## Where things go

| Change | Files |
| --- | --- |
| New tool | `src/snow_mcp/server.py`, the backend contract in `backends/base.py`, both backends, plus a test in `tests/test_mcp_server.py` |
| New eval task | `src/snow_mcp/evals/tasks.yaml` — see [docs/EVALS.md](docs/EVALS.md) |
| New fixture data | `src/snow_mcp/data/seed.json`; update the counts in `test_seed_loads_all_tables` |
| Prompt changes | `src/snow_mcp/agent/prompts.py`; measure the effect with `snow-evals --prompt <variant>` |

## Adding a tool

A tool is only finished when its description says *when not to use it*. The tool descriptions are
prompts — they are the primary lever on tool-selection accuracy, more so than the system prompt.
Add at least one eval task that can distinguish the new tool from its nearest neighbour, or the
metric cannot tell you whether the description works.

## Conventions

- Domain errors are returned as recoverable JSON, not raised through the protocol. Error text should
  tell the agent what to do differently.
- Anything that could invalidate a record goes in the backend, not the prompt.
- Reads must not mutate the frozen clock — use `Clock.peek()`.
- Never `print()` in the server process; stdout carries JSON-RPC. Log to stderr.
