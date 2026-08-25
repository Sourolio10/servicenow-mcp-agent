"""Command line entry point for the ITSM agent.

    snow-agent "Resolve INC0010003 using the documented VPN fix"
    snow-agent --interactive
    snow-agent --transport stdio "Which group has the most open P1s?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from ..config import Settings
from ..server import build_server
from .agent import ITSMAgent
from .bridge import MCPToolBridge, stdio_target
from .llm import DEFAULT_MODEL, AnthropicLLM


def _build_target(args: argparse.Namespace) -> Any:
    """Choose how the agent reaches the MCP server."""
    if args.url:
        return args.url
    if args.transport == "stdio":
        env = {
            key: value
            for key, value in os.environ.items()
            if key.startswith("SNOW_") or key in ("PATH", "HOME", "PYTHONPATH")
        }
        return stdio_target(sys.executable, ["-m", "snow_mcp.server"], env)
    return build_server(settings=Settings.from_env())


def _render(run: Any, verbose: bool) -> None:
    if verbose:
        for call in run.tool_calls:
            status = "ok " if call.ok else "ERR"
            print(f"  [{status}] {call.name}({json.dumps(call.arguments, default=str)[:110]}) "
                  f"{call.latency_ms:.0f}ms", file=sys.stderr)
    print(run.answer or "(no answer)")
    if run.error:
        print(f"\n[error] {run.error}", file=sys.stderr)
    print(
        f"\n— {len(run.tool_calls)} tool calls, {run.total_tool_ms:.0f}ms in MCP, "
        f"{run.total_model_ms:.0f}ms in model, {run.input_tokens}+{run.output_tokens} tokens",
        file=sys.stderr,
    )


async def _run(args: argparse.Namespace) -> int:
    llm = AnthropicLLM(model=args.model, effort=args.effort)
    target = _build_target(args)

    async with MCPToolBridge(target=target) as bridge:
        if args.list_tools:
            for spec in bridge.tool_specs:
                print(f"{spec['name']}\n    {spec['description'].splitlines()[0]}")
            return 0

        agent = ITSMAgent(llm, bridge, prompt_variant=args.prompt, max_turns=args.max_turns)

        if args.interactive:
            print(f"ITSM agent ready ({len(bridge.tool_names)} tools, model {args.model}). "
                  "Ctrl-C or 'exit' to quit.\n")
            while True:
                try:
                    task = input("you> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 0
                if task.lower() in ("exit", "quit"):
                    return 0
                if not task:
                    continue
                run = await agent.run(task)
                print()
                _render(run, args.verbose)
                print()
        else:
            run = await agent.run(args.task)
            if args.json:
                print(json.dumps(run.to_dict(), indent=2))
            else:
                _render(run, args.verbose)
            return 1 if run.error else 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude agent driving the ITSM MCP server")
    parser.add_argument("task", nargs="?", default="", help="task to perform")
    parser.add_argument("-i", "--interactive", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=None, choices=["low", "medium", "high"],
                        help="model effort level")
    parser.add_argument("--prompt", default="operator", choices=["operator", "minimal"])
    parser.add_argument("--transport", default="in-process", choices=["in-process", "stdio"],
                        help="in-process is faster; stdio exercises the real subprocess path")
    parser.add_argument("--url", default=None, help="connect to an MCP server over HTTP instead")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--list-tools", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true", help="print each tool call")
    parser.add_argument("--json", action="store_true", help="emit the full run record as JSON")
    args = parser.parse_args(argv)

    if not args.task and not args.interactive and not args.list_tools:
        parser.error("provide a task, or use --interactive / --list-tools")

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
