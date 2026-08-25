"""Eval CLI.

    snow-evals                                   # full suite, default model
    snow-evals --tasks resolve-vpn-with-kb       # one task
    snow-evals --category cmdb --concurrency 4
    snow-evals --prompt minimal --out runs/minimal   # prompt ablation
    snow-evals --list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..agent.llm import DEFAULT_MODEL
from ..config import Settings
from .report import to_markdown, write_reports
from .runner import EvalRunner, TaskResult, load_tasks, summarise


def _progress(result: TaskResult) -> None:
    verdict = "PASS" if result.completed else "FAIL"
    detail = ""
    if not result.completed:
        reasons = []
        if result.selection.forbidden_used:
            reasons.append("forbidden " + ",".join(result.selection.forbidden_used))
        if result.failed_checks:
            reasons.append(f"{len(result.failed_checks)} check(s)")
        if result.run.error:
            reasons.append(str(result.run.error)[:60])
        detail = "  (" + "; ".join(reasons) + ")"
    print(
        f"  {verdict}  {result.task.id:<32} f1={result.selection.f1:.2f} "
        f"calls={len(result.run.tool_calls):<2} mcp={result.run.total_tool_ms:6.1f}ms{detail}",
        file=sys.stderr,
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the ITSM MCP agent")
    parser.add_argument("--tasks", nargs="*", default=[], help="task ids to run")
    parser.add_argument("--category", nargs="*", default=[], help="categories to run")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=None, choices=["low", "medium", "high"])
    parser.add_argument("--prompt", default="operator", choices=["operator", "minimal"])
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--out", default="runs/latest", help="output directory for reports")
    parser.add_argument("--list", action="store_true", help="list tasks and exit")
    parser.add_argument("--fail-under", type=float, default=None,
                        help="exit non-zero if completion rate falls below this (0-1)")
    args = parser.parse_args(argv)

    tasks = load_tasks(only=args.tasks, categories=args.category)
    if args.list:
        for task in tasks:
            print(f"{task.id:<34} {task.category:<11} {task.difficulty:<7} {task.prompt[:70]}")
        return 0
    if not tasks:
        print("no tasks matched", file=sys.stderr)
        return 2

    def llm_factory():
        from ..agent.llm import AnthropicLLM

        return AnthropicLLM(model=args.model, effort=args.effort)

    out_dir = Path(args.out)
    runner = EvalRunner(
        llm_factory,
        prompt_variant=args.prompt,
        max_turns=args.max_turns,
        settings=Settings.from_env(),
        trace_path=out_dir / "traces.jsonl",
    )

    print(f"running {len(tasks)} task(s) · model={args.model} · prompt={args.prompt} "
          f"· concurrency={args.concurrency}\n", file=sys.stderr)

    import asyncio

    try:
        results = asyncio.run(
            runner.run_suite(tasks, concurrency=args.concurrency, on_result=_progress)
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = summarise(
        results,
        metadata={
            "model": args.model,
            "prompt_variant": args.prompt,
            "backend": Settings.from_env().backend,
            "transport": "in-process",
            "suite_size": len(tasks),
            "concurrency": args.concurrency,
        },
    )
    paths = write_reports(report, out_dir)
    print("\n" + to_markdown(report))
    print(f"\nwrote {paths['markdown']}, {paths['json']}, {paths['html']}, "
          f"{out_dir / 'traces.jsonl'}", file=sys.stderr)

    rate = report["summary"]["task_completion"]["rate"]
    if args.fail_under is not None and rate < args.fail_under:
        print(f"completion rate {rate:.2%} below threshold {args.fail_under:.2%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
