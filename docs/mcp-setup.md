# MCP setup for desktop agents

The server speaks MCP over stdio: the agent launches it as a subprocess.
ClickHouse credentials come from the environment (`OSSIE_CLICKHOUSE_URL`,
default `http://127.0.0.1:8123`) or `--url`. Everything the agent sees and
runs is limited to that ClickHouse user.

## Claude Desktop

Add to `claude_desktop_config.json` (Settings, Developer, Edit Config).
Replace the three placeholders: the absolute path of this repository, the
absolute path of the model, and the ClickHouse URL with the credentials of
the user the agent should act as. Desktop apps run with a minimal `PATH`,
so give `uv` as an absolute path (`which uv`) if it is not found.

```json
{
  "mcpServers": {
    "ossie-clickhouse": {
      "command": "/ABSOLUTE/PATH/TO/uv",
      "args": ["run", "--directory", "/ABSOLUTE/PATH/TO/ossie-clickhouse",
               "ossie-clickhouse", "serve", "/ABSOLUTE/PATH/TO/model.yaml"],
      "env": {"OSSIE_CLICKHOUSE_URL": "http://USER:PASSWORD@127.0.0.1:8123"}
    }
  }
}
```

Restart the app. If it reports "Server disconnected", open
`~/Library/Logs/Claude/mcp-server-ossie-clickhouse.log`: a placeholder left
in place shows up as `No such file or directory`. The server appears with
four tools: `list_model`,
`search_model`, `describe_object`, `query`. Ask in business terms, for
example "sales by item category in 1998"; the agent should call
`list_model`, maybe `describe_object`, then `query`.

## Claude Code and other clients

Any MCP client that supports stdio works the same way. For Claude Code:

```bash
claude mcp add ossie-clickhouse -e OSSIE_CLICKHOUSE_URL=http://127.0.0.1:8123 -- \
  uv run --directory /path/to/ossie-clickhouse ossie-clickhouse serve /path/to/model.yaml
```

## Checking without an agent

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | ossie-clickhouse serve model.yaml
```

## Access

The process runs as one ClickHouse user. Give each agent its own user with
the grants and row policies it should have; the model it sees is trimmed
to match. `--policy policy.yaml` hides further objects per user or role.
A remote, multi-user server with OAuth is planned (roadmap, Phase 6b).
