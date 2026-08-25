#!/usr/bin/env bash
# A guided tour of the project. Requires ANTHROPIC_API_KEY for steps 3 onward.
set -euo pipefail

blue() { printf '\n\033[1;34m== %s ==\033[0m\n' "$1"; }

blue "1. The tool surface (no API key needed)"
python - <<'PY'
import asyncio
from mcp import Client
from snow_mcp.server import build_server

async def main():
    async with Client(build_server()) as client:
        tools = (await client.list_tools()).tools
        print(f"{len(tools)} tools exposed over MCP:\n")
        for tool in tools:
            print(f"  {tool.name:<24} {(tool.description or '').splitlines()[0][:64]}")
asyncio.run(main())
PY

blue "2. A tool call, straight over the protocol"
python - <<'PY'
import asyncio, json
from mcp import Client
from snow_mcp.server import build_server

async def main():
    async with Client(build_server()) as client:
        result = await client.call_tool(
            "get_ci_relationships",
            {"name": "SAN-ARRAY-01", "direction": "downstream", "depth": 3},
        )
        payload = json.loads(result.content[0].text)
        print("If SAN-ARRAY-01 fails, these are affected:")
        for item in payload["downstream"]:
            print(f"  {item['hops']} hop(s)  {item['name']:<18} {item['business_criticality']}")
asyncio.run(main())
PY

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo -e "\nSet ANTHROPIC_API_KEY to continue with the agent and eval steps."
  exit 0
fi

blue "3. The agent doing multi-step CMDB reasoning"
snow-agent -v "The payment service PAY-APP-01 is failing. What does it depend on, and is anything already broken underneath it?"

blue "4. The agent declining to create a duplicate"
snow-agent -v "A manager reports the checkout page is throwing 502 errors for everyone. Log it."

blue "5. A slice of the eval suite"
snow-evals --category cmdb safety --concurrency 2 --out runs/demo

echo -e "\nReport written to runs/demo/report.md (and .html / .json / traces.jsonl)"
