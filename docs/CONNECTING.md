# Connecting the MCP server to a client

The server speaks stdio by default, which is what Claude Desktop and Claude Code expect.

## Claude Code

```bash
cd servicenow-mcp-agent
claude mcp add servicenow-itsm -- python -m snow_mcp.server
```

Or commit `.mcp.json` (already in this repo) and Claude Code will pick it up for anyone who clones
the project:

```json
{
  "mcpServers": {
    "servicenow-itsm": {
      "command": "python",
      "args": ["-m", "snow_mcp.server"],
      "env": { "SNOW_BACKEND": "mock" }
    }
  }
}
```

Verify with `/mcp` inside Claude Code — you should see 14 tools.

## Claude Desktop

Add to `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "servicenow-itsm": {
      "command": "/absolute/path/to/servicenow-mcp-agent/.venv/bin/python",
      "args": ["-m", "snow_mcp.server"],
      "env": {
        "SNOW_BACKEND": "mock",
        "SNOW_READ_ONLY": "0"
      }
    }
  }
}
```

Use an **absolute** path to the virtualenv's Python — Claude Desktop does not inherit your shell's
environment. Restart the app after editing. A copy of this file is in
`examples/claude_desktop_config.json`.

## HTTP transport

For a remote or shared deployment:

```bash
snow-mcp-server --transport streamable-http --port 8000
```

The agent can then connect with `snow-agent --url http://127.0.0.1:8000/mcp "..."`.

There is no authentication in front of the HTTP transport. Put it behind a reverse proxy that
terminates TLS and authenticates, or keep it on localhost.

## Debugging

```bash
snow-mcp-server --log-level DEBUG      # logs go to stderr; stdout is reserved for JSON-RPC
npx @modelcontextprotocol/inspector python -m snow_mcp.server
```

If the server appears to start and immediately disconnect, something is writing to stdout. Any
`print()` in the server process corrupts the JSON-RPC stream — that is why all logging in this repo
is configured to stderr.

## Read-only mode

```bash
snow-mcp-server --read-only
```

All six write tools return a refusal that tells the agent to report the intended change instead of
attempting it. Recommended when first connecting to a real instance.
