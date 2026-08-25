"""Render an eval report as Markdown and as a standalone HTML page."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    meta = report.get("metadata", {})
    selection = summary.get("tool_selection", {})
    completion = summary.get("task_completion", {})
    latency = summary.get("latency", {})
    reliability = summary.get("reliability", {})
    cost = summary.get("cost", {})

    call = latency.get("per_tool_call_ms", {})
    turn = latency.get("per_model_turn_ms", {})
    wall = latency.get("per_task_wall_ms", {})

    lines: list[str] = []
    lines.append("# ITSM MCP agent — eval report\n")
    lines.append(
        f"`{meta.get('model', 'unknown')}` · prompt `{meta.get('prompt_variant', '?')}` · "
        f"backend `{meta.get('backend', '?')}` · transport `{meta.get('transport', '?')}` · "
        f"{meta.get('generated_at', '')}\n"
    )

    lines.append("## Headline\n")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Task completion rate | **{_pct(completion.get('rate', 0))}** "
                 f"({completion.get('completed', 0)}/{summary.get('tasks', 0)}) |")
    lines.append(f"| Tool-selection F1 (macro) | **{_pct(selection.get('macro_f1', 0))}** |")
    lines.append(f"| Tool-selection precision / recall | {_pct(selection.get('macro_precision', 0))} / "
                 f"{_pct(selection.get('macro_recall', 0))} |")
    lines.append(f"| Exact tool-set match | {_pct(selection.get('exact_set_match_rate', 0))} |")
    lines.append(f"| First-tool accuracy | {_pct(selection.get('first_tool_accuracy', 0))} |")
    lines.append(f"| Forbidden-tool rate | {_pct(selection.get('forbidden_rate', 0))} |")
    lines.append(f"| Latency per tool call (p50 / p95 / max) | {call.get('p50_ms', 0)} / "
                 f"{call.get('p95_ms', 0)} / {call.get('max_ms', 0)} ms |")
    lines.append(f"| Latency per model turn (p50 / p95) | {turn.get('p50_ms', 0)} / {turn.get('p95_ms', 0)} ms |")
    lines.append(f"| Wall clock per task (p50 / p95) | {wall.get('p50_ms', 0)} / {wall.get('p95_ms', 0)} ms |")
    lines.append(f"| Tool error rate | {_pct(reliability.get('tool_error_rate', 0))} "
                 f"({reliability.get('failed_tool_calls', 0)}/{reliability.get('total_tool_calls', 0)}) |")
    lines.append(f"| Mean tool calls per task | {reliability.get('mean_calls_per_task', 0)} |")
    lines.append(f"| Tokens (in / out) | {cost.get('input_tokens', 0):,} / {cost.get('output_tokens', 0):,} |")
    lines.append("")

    by_category = summary.get("by_category", {})
    if by_category:
        lines.append("## By category\n")
        lines.append("| Category | Tasks | Completion | Mean tool F1 |")
        lines.append("| --- | ---: | ---: | ---: |")
        for name, bucket in sorted(by_category.items()):
            lines.append(
                f"| {name} | {bucket['tasks']} | {_pct(bucket['completion_rate'])} | "
                f"{_pct(bucket['mean_f1'])} |"
            )
        lines.append("")

    by_tool = latency.get("by_tool", {})
    if by_tool:
        lines.append("## Latency per tool (MCP round trip)\n")
        lines.append("| Tool | Calls | Mean | p50 | p95 | Max |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for name, stats in sorted(by_tool.items(), key=lambda item: -item[1]["count"]):
            lines.append(
                f"| `{name}` | {stats['count']} | {stats['mean_ms']} | {stats['p50_ms']} | "
                f"{stats['p95_ms']} | {stats['max_ms']} |"
            )
        lines.append("")

    lines.append("## Per task\n")
    lines.append("| Task | Category | Pass | Tool F1 | Calls | MCP ms | Model ms | Notes |")
    lines.append("| --- | --- | :---: | ---: | ---: | ---: | ---: | --- |")
    for task in report.get("tasks", []):
        notes: list[str] = []
        if task.get("forbidden_used"):
            notes.append("forbidden: " + ", ".join(task["forbidden_used"]))
        if task.get("tools_missing"):
            notes.append("missing: " + ", ".join(task["tools_missing"]))
        if task.get("tools_extra"):
            notes.append("extra: " + ", ".join(task["tools_extra"]))
        if task.get("failed_checks"):
            notes.append(f"{len(task['failed_checks'])} check(s) failed")
        if task.get("error"):
            notes.append(str(task["error"])[:80])
        lines.append(
            f"| `{task['id']}` | {task['category']} | {'PASS' if task['completed'] else 'FAIL'} | "
            f"{task['tool_f1']:.2f} | {task['calls']} | {task['tool_ms']:.0f} | "
            f"{task['model_ms']:.0f} | {'; '.join(notes) or '—'} |"
        )
    lines.append("")

    failures = [task for task in report.get("tasks", []) if not task["completed"]]
    if failures:
        lines.append("## Failure detail\n")
        for task in failures:
            lines.append(f"**`{task['id']}`** — tools called: "
                         f"{', '.join(f'`{name}`' for name in task['tools_called']) or 'none'}")
            for check in task.get("failed_checks", []):
                lines.append(f"  - failed: {check}")
            if task.get("error"):
                lines.append(f"  - error: {task['error']}")
            lines.append("")

    return "\n".join(lines)


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ITSM MCP agent — eval report</title>
<style>
  :root {{ color-scheme: light dark; --fg:#1a1a1a; --muted:#666; --line:#e2e2e2;
           --pass:#1a7f37; --fail:#b42318; --bg:#fff; --card:#fafafa; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg:#e8e8e8; --muted:#9a9a9a; --line:#333; --bg:#131313; --card:#1c1c1c;
             --pass:#3fb950; --fail:#f85149; }}
  }}
  body {{ margin:0; padding:2.5rem 1.5rem; background:var(--bg); color:var(--fg);
          font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:1040px; margin:0 auto; }}
  h1 {{ font-size:1.6rem; margin:0 0 .25rem; letter-spacing:-.02em; }}
  .meta {{ color:var(--muted); font-size:.85rem; margin-bottom:2rem; font-family:ui-monospace,monospace; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:.75rem; margin-bottom:2rem; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:.9rem 1rem; }}
  .card .label {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }}
  .card .value {{ font-size:1.7rem; font-weight:600; letter-spacing:-.03em; margin-top:.2rem; }}
  .card .sub {{ font-size:.78rem; color:var(--muted); }}
  h2 {{ font-size:1.05rem; margin:2rem 0 .6rem; padding-bottom:.35rem; border-bottom:1px solid var(--line); }}
  table {{ width:100%; border-collapse:collapse; font-size:.86rem; }}
  th {{ text-align:left; font-weight:600; color:var(--muted); font-size:.75rem;
        text-transform:uppercase; letter-spacing:.05em; padding:.4rem .5rem; border-bottom:1px solid var(--line); }}
  td {{ padding:.42rem .5rem; border-bottom:1px solid var(--line); vertical-align:top; }}
  td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  code {{ font-family:ui-monospace,monospace; font-size:.85em; }}
  .pass {{ color:var(--pass); font-weight:600; }}
  .fail {{ color:var(--fail); font-weight:600; }}
  .bar {{ height:5px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:.35rem; }}
  .bar > i {{ display:block; height:100%; background:var(--pass); }}
  .notes {{ color:var(--muted); font-size:.8rem; }}
</style>
</head>
<body><main>
<h1>ITSM MCP agent — eval report</h1>
<div class="meta">{meta}</div>
<div class="cards">{cards}</div>
{sections}
</main></body></html>
"""


def _card(label: str, value: str, sub: str = "", ratio: float | None = None) -> str:
    bar = f'<div class="bar"><i style="width:{max(0.0, min(1.0, ratio)) * 100:.0f}%"></i></div>' if ratio is not None else ""
    return (
        f'<div class="card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div>'
        f'<div class="sub">{html.escape(sub)}</div>{bar}</div>'
    )


def _table(headers: list[str], rows: list[list[str]], numeric_from: int = 1) -> str:
    head = "".join(
        f'<th class="num">{html.escape(name)}</th>' if index >= numeric_from else f"<th>{html.escape(name)}</th>"
        for index, name in enumerate(headers)
    )
    body = ""
    for row in rows:
        cells = "".join(
            f'<td class="num">{cell}</td>' if index >= numeric_from else f"<td>{cell}</td>"
            for index, cell in enumerate(row)
        )
        body += f"<tr>{cells}</tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def to_html(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    meta = report.get("metadata", {})
    selection = summary.get("tool_selection", {})
    completion = summary.get("task_completion", {})
    latency = summary.get("latency", {})
    reliability = summary.get("reliability", {})
    call = latency.get("per_tool_call_ms", {})

    cards = "".join([
        _card("Task completion", _pct(completion.get("rate", 0)),
              f"{completion.get('completed', 0)} of {summary.get('tasks', 0)} tasks",
              completion.get("rate", 0)),
        _card("Tool-selection F1", _pct(selection.get("macro_f1", 0)),
              f"P {_pct(selection.get('macro_precision', 0))} · R {_pct(selection.get('macro_recall', 0))}",
              selection.get("macro_f1", 0)),
        _card("First-tool accuracy", _pct(selection.get("first_tool_accuracy", 0)),
              f"exact set {_pct(selection.get('exact_set_match_rate', 0))}",
              selection.get("first_tool_accuracy", 0)),
        _card("MCP latency p50", f"{call.get('p50_ms', 0)} ms",
              f"p95 {call.get('p95_ms', 0)} ms · max {call.get('max_ms', 0)} ms"),
        _card("Tool error rate", _pct(reliability.get("tool_error_rate", 0)),
              f"{reliability.get('failed_tool_calls', 0)} of {reliability.get('total_tool_calls', 0)} calls"),
        _card("Calls per task", str(reliability.get("mean_calls_per_task", 0)),
              f"{reliability.get('total_tool_calls', 0)} total"),
    ])

    sections: list[str] = []

    by_category = summary.get("by_category", {})
    if by_category:
        rows = [
            [html.escape(name), str(bucket["tasks"]), _pct(bucket["completion_rate"]), _pct(bucket["mean_f1"])]
            for name, bucket in sorted(by_category.items())
        ]
        sections.append("<h2>By category</h2>" + _table(["Category", "Tasks", "Completion", "Mean tool F1"], rows))

    by_tool = latency.get("by_tool", {})
    if by_tool:
        rows = [
            [f"<code>{html.escape(name)}</code>", str(stats["count"]), f"{stats['mean_ms']}",
             f"{stats['p50_ms']}", f"{stats['p95_ms']}", f"{stats['max_ms']}"]
            for name, stats in sorted(by_tool.items(), key=lambda item: -item[1]["count"])
        ]
        sections.append("<h2>Latency per tool (ms, MCP round trip)</h2>"
                        + _table(["Tool", "Calls", "Mean", "p50", "p95", "Max"], rows))

    rows = []
    for task in report.get("tasks", []):
        notes: list[str] = []
        if task.get("forbidden_used"):
            notes.append("forbidden: " + ", ".join(task["forbidden_used"]))
        if task.get("tools_missing"):
            notes.append("missing: " + ", ".join(task["tools_missing"]))
        if task.get("failed_checks"):
            notes.append(f"{len(task['failed_checks'])} check(s) failed")
        verdict = '<span class="pass">PASS</span>' if task["completed"] else '<span class="fail">FAIL</span>'
        rows.append([
            f"<code>{html.escape(task['id'])}</code>",
            html.escape(task["category"]),
            verdict,
            f"{task['tool_f1']:.2f}",
            str(task["calls"]),
            f"{task['tool_ms']:.0f}",
            f"{task['model_ms']:.0f}",
            f'<span class="notes">{html.escape("; ".join(notes)) or "—"}</span>',
        ])
    sections.append(
        "<h2>Per task</h2>"
        + _table(["Task", "Category", "Result", "Tool F1", "Calls", "MCP ms", "Model ms", "Notes"], rows, numeric_from=2)
    )

    meta_line = " · ".join(
        f"{key}={value}" for key, value in meta.items() if key != "generated_at"
    ) + f" · {meta.get('generated_at', '')}"

    return _HTML_TEMPLATE.format(meta=html.escape(meta_line), cards=cards, sections="".join(sections))


def write_reports(report: dict[str, Any], out_dir: Path | str) -> dict[str, Path]:
    """Write report.json, report.md and report.html; return the paths."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": directory / "report.json",
        "markdown": directory / "report.md",
        "html": directory / "report.html",
    }
    paths["json"].write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    paths["markdown"].write_text(to_markdown(report), encoding="utf-8")
    paths["html"].write_text(to_html(report), encoding="utf-8")
    return paths
