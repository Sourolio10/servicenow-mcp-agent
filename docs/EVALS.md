# Evals

## Running

```bash
snow-evals                                     # all 24 tasks
snow-evals --list                              # show the suite
snow-evals --tasks resolve-vpn-with-kb cmdb-blast-radius
snow-evals --category triage safety
snow-evals --concurrency 4                     # tasks are independent
snow-evals --prompt minimal --out runs/minimal # prompt ablation
snow-evals --fail-under 0.8                    # non-zero exit below the threshold
```

Each run writes to `--out` (default `runs/latest`):

| File | Contents |
| --- | --- |
| `report.md` | headline table, per-category, per-tool latency, per-task results, failure detail |
| `report.html` | the same as a standalone page with summary cards |
| `report.json` | the full structured report |
| `traces.jsonl` | one line per task: every tool call, its arguments, latency, and result preview |

## Metric definitions

### Tool-selection accuracy

For each task, take the **set of distinct tools** the agent called and compare it to
`expected_tools`. Set-based rather than sequence-based, because several orderings are legitimately
correct — ordering is captured separately by `first_tool_correct`.

```
hits      = expected ∩ called
judged    = called − optional        # optional tools are not held against the agent
precision = |hits| / |judged|
recall    = |hits| / |expected|
F1        = harmonic mean
```

Macro-averaged across tasks, so a task with eight calls does not outweigh a task with one.

Also reported:

- **exact_set_match** — the judged set equals the expected set exactly
- **first_tool_accuracy** — the opening call was acceptable. This is where a misread of the request
  shows up: an agent that starts with `create_incident` on a duplicate-detection task has already
  lost, whatever it does next
- **forbidden_rate** — fraction of tasks that touched a tool listed in `forbidden_tools`

Three vocabularies, deliberately:

| Field | Meaning | Effect |
| --- | --- | --- |
| `expected_tools` | a correct solution needs these | drives recall |
| `optional_tools` | defensible but not required | excluded from the precision denominator |
| `forbidden_tools` | using it is a real mistake | flagged, and fails the task |

Without `optional_tools`, an agent that sensibly calls `lookup_user` before `update_incident` would
be scored as imprecise. Without `forbidden_tools`, an agent that creates a duplicate incident and
then describes it fluently would pass.

### Task-completion rate

A task passes when **all** of the following hold:

1. no forbidden tool was called
2. every `state_check` assertion passes
3. every `answer_check` assertion passes

`state_check`s run **after** the agent finishes, as read-only tool calls **through the same MCP
session** — not by reaching into the store. This matters twice: it proves the change is visible over
the protocol, and it means the identical checks work against a live ServiceNow instance. Grader
calls are excluded from the tool-selection and latency metrics.

An agent that produces a confident summary without making the required change scores zero.
`test_end_to_end_eval_run_fails_a_lying_agent` asserts this.

### Latency per call

**MCP round-trip latency** per tool call — serialise, transport, server handler, backend, response —
measured in `MCPToolBridge.call`. Reported as mean / p50 / p95 / max, overall and broken down per
tool name. Model latency and wall clock are reported separately so transport cost is never confused
with model cost.

Percentiles use nearest-rank, which is stable for the small samples an eval produces.

## Adding a task

Append to `src/snow_mcp/evals/tasks.yaml`:

```yaml
  - id: triage-escalate-vip
    category: triage
    difficulty: medium
    prompt: >-
      Dana Whitfield is a VIP and her VPN ticket has not moved. Escalate it.
    expected_tools: [update_incident]
    optional_tools: [lookup_user, search_incidents, get_incident]
    forbidden_tools: [resolve_incident]
    state_checks:
      - tool: get_incident
        args: {number: "INC0010003"}
        expect:
          - path: urgency
            equals: "1"
    answer_checks:
      - contains: "INC0010003"
```

`test_suite_loads_and_is_internally_consistent` will fail if the task references a tool that does
not exist, lists a tool as both expected and forbidden, or declares no graded checks.

### Assertion operators

`equals`, `equals_any`, `contains`, `contains_all`, `contains_any`, `not_contains`, `matches`
(regex), `not_matches`, `length`, `min_length`, `gte`, `lte`.

Paths are dotted with list support: `work_notes.*.value`, `incidents.0.number`,
`incidents.caller_id` (collects across a list).

## Interpreting a result

- **High recall, low precision** → the agent works but wanders. Look at `tools_extra`; usually two
  tool descriptions overlap and need sharpening.
- **High tool F1, low completion** → the right tools with the wrong arguments. Check `traces.jsonl`
  for the actual call payloads.
- **Low first-tool accuracy** → the agent is misreading intent, not misusing tools. That is a system
  prompt problem; compare `--prompt operator` against `--prompt minimal`.
- **Forbidden-tool hits** → a judgment failure, the most serious category. These are the tasks worth
  reading the full trace on.
