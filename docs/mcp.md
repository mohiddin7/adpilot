# MCP server

AdPilot speaks the [Model Context Protocol](https://modelcontextprotocol.io), so Claude Desktop, Claude Code or
any MCP client can query the data set directly. It is the same agent as the CLI and the HTTP API: every question
goes through `ask()`, with the same guard chain, model-fallback chain, output redaction and audit record.

## Tools

| tool | arguments | returns |
|---|---|---|
| `ask` | `question` (1–4000 chars), optional `session_id` (`[A-Za-z0-9._-]{1,64}`) | the same `AnswerBody` as `POST /ask`: `trace_id`, `answer_md`, `sql`, `data`, `chart`, `confidence`, `caveats`, `refused` |
| `schema` | — | the tables and columns the agent can query, as text |

Both are annotated read-only. There is no `connector` argument: the data source is server-side configuration.

**Why not expose `run_sql` and the other agent tools?** That would make the MCP client the analyst: its model
would write the SQL, outside the agent's prompt, pack glossary and self-repair loop, and a second path to the data
source would sit beside the guarded one. `ask` gives the client the analyst's answer, with the SQL and rows it
used, which covers what a client needs to show its work.

## Local: stdio

```bash
pip install -e ".[mcp]"
ADPILOT_AUDIT=memory adpilot --connector duckdb mcp   # speaks MCP on stdin/stdout; status lines go to stderr
```

Claude Code:

```bash
claude mcp add adpilot -e ADPILOT_AUDIT=memory -- /absolute/path/to/adpilot/.venv/bin/adpilot --connector duckdb mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "adpilot": {
      "command": "/absolute/path/to/adpilot/.venv/bin/adpilot",
      "args": ["--connector", "duckdb", "mcp"],
      "env": { "ADPILOT_AUDIT": "memory" }
    }
  }
}
```

The server starts from whatever directory the client picks; the pack and the bundled CSVs are found relative
to the installed package, not the working directory. `.env` is read as for every other command, so
`AGENT_LLM_BEARER_TOKEN` there is enough; without it every answer comes from the pack's pre-defined queries
and says so.

Like `adpilot chat`, the process refuses to start if the audit store is unreachable. `ADPILOT_AUDIT=memory`
(above) runs it unrecorded, and says so on stderr; drop it once the audit dataset is set up
(see [observability.md](observability.md)) and every MCP call is recorded.

## Remote: `/mcp` on the API

The HTTP API (see [api.md](api.md)) serves the same two tools at `/mcp` over streamable HTTP, behind the same
`X-API-Key`:

```bash
claude mcp add --transport http adpilot https://<service-url>/mcp --header "X-API-Key: $ADPILOT_API_KEY"
```

- **Stateless, JSON responses, POST only.** Each call stands alone, so any instance can serve it. There are no
  server-initiated messages, so `GET /mcp` (the optional server-to-client event stream) is a 405 rather than a
  connection held open per client.
- **The API's rate limit applies.** `/ask`, `/ask/stream`, `/schema` and both MCP tools share one
  `ADPILOT_API_RPM` bucket. (Over stdio there is no request limit: it is one local user; model calls are still
  throttled inside `ask()`.)
- **Use `/mcp`, not `/mcp/`.** The trailing-slash form redirects.
- **Any `Host` is accepted.** The SDK's DNS-rebinding check is a localhost allowlist that would reject the
  deployed host; the attack it defends against needs the API key, which the key gate already requires.
- The path exists only when the image has the `mcp` extra (the Dockerfile installs `.[api,mcp]`). Without
  it the API logs a warning at startup and `/mcp` is a 404.

## What a client sees when something goes wrong

Tool errors reach the client as text prefixed by the SDK, e.g. `Error executing tool ask: rate limit exceeded —
retry in 13 s`; the table gives the part after the prefix.

| condition | result |
|---|---|
| missing / wrong key (HTTP) | `401`, no audit row |
| question >4000 chars, bad `session_id` | tool error (`isError: true`), no audit row |
| over the rate limit (HTTP, `ask` or `schema`) | tool error `rate limit exceeded — retry in N s`, no audit row |
| question >600 chars, injection shape, secret name | a normal result with `refused: true` and an `InputPolicy` caveat, audited |
| out of scope | a normal result with `refused: true`, audited |
| model unavailable / 429 / budget exceeded | a normal result from the rule-based fallback, with a caveat naming why, audited |
| any other server fault | tool error `internal error — the server log has the details`; the cause is only in the server log |

A refusal is an answer, not an error, exactly as in the CLI and the HTTP API.

## Audit

Every `ask` and every `schema` call is one `agent_calls` row with `source = 'mcp'`. A schema read has
`case_name = 'schema'`, `question = '(schema)'` and an empty answer — it records who read the schema and when, not
the text. A `session_id` continues across surfaces: a session started in `adpilot chat` or over `/ask` can be
continued over MCP with the same id, and the other way round.
