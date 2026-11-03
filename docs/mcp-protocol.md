# The MCP protocol, frame by frame

This is a walkthrough of what this project's client and server actually put on
the wire. Run `uv run react-mcp protocol-demo` in another terminal to see the
same sequence printed live. Every frame is also appended to
`traces/<run>/protocol.jsonl`.

## Roles and transports

- **Host** - the application the user interacts with (this CLI).
- **Client** - one logical connection to one server. Here: `SDKMCPClient` or
  `RawMCPClient`.
- **Server** - the process exposing tools, resources, and prompts. Here:
  `python -m react_mcp.server`.

Two transports are supported:

| Transport | How it works | When this project uses it |
| --- | --- | --- |
| stdio | Client launches the server and exchanges newline-delimited JSON on stdin/stdout. stderr is for logs. | Default for `ask`, `tools`, `call`, `repl`, `protocol-demo`. |
| Streamable HTTP | Client POSTs JSON-RPC messages to an HTTP endpoint; the server may stream responses. | `react-mcp serve --http` plus `--transport http`. |

stdio frames are just UTF-8 JSON followed by `\n`. There is no length prefix and
no Content-Length header. A frame must not contain a literal newline, so JSON
serializers escape newlines inside strings.

## Lifecycle

### 1. initialize

The client opens with its protocol version, capabilities, and identity:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "protocolVersion":"2025-11-25",
  "capabilities":{},
  "clientInfo":{"name":"react-mcp-raw-client","version":"0.1.0"}
}}
```

A server that serves the legacy handshake replies:

```json
{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":"2025-11-25",
  "capabilities":{
    "tools":{"listChanged":true},
    "resources":{"subscribe":false,"listChanged":true},
    "prompts":{"listChanged":true},
    "logging":{}
  },
  "serverInfo":{"name":"ReAct Tools","version":"0.1.0"},
  "instructions":"Local learning server for a ReAct agent..."
}}
```

The client must then confirm:

```json
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

Notifications have **no `id`** and get no response. During initialization the
client can also send `notifications/initialized` only after it has received a
successful `initialize` result.

### 2. Newer discovery

Recent MCP revisions added `server/discover`, which lets a client ask what the
server supports before choosing an interaction mode. The official SDK client
used here negotiates `2026-07-28` automatically; the raw client deliberately
uses `initialize` with `2025-11-25` because most documentation and tooling still
describe that handshake. The server accepts both eras, which is why the same
subprocess works for `SDKMCPClient` and `RawMCPClient`.

The core rule is unchanged: both sides must agree on a protocol version, and the
client must only use capabilities the server advertised.

### 3. Discovery

```json
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

```json
{"jsonrpc":"2.0","id":2,"result":{"tools":[
  {
    "name":"calculator",
    "title":"Calculator",
    "description":"Evaluate a basic arithmetic expression safely.",
    "inputSchema":{
      "type":"object",
      "properties":{"expression":{"type":"string","description":"Arithmetic expression..."}},
      "required":["expression"]
    },
    "annotations":{"readOnlyHint":true,"openWorldHint":false}
  }
]}}
```

`inputSchema` is JSON Schema generated from the Python signature. The agent
turns it into prompt text, which is why `Annotated[..., Field(description=...)]`
matters. `annotations` are hints for hosts, not security boundaries.

List methods may return `nextCursor`; clients should paginate until it is
absent. This project has few enough items that it does not need to.

Resources and prompts are discovered the same way:

```json
{"jsonrpc":"2.0","id":3,"method":"resources/list","params":{}}
{"jsonrpc":"2.0","id":4,"method":"prompts/list","params":{}}
```

### 4. Calling a tool

```json
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{
  "name":"calculator",
  "arguments":{"expression":"(17 * 3) + 9"}
}}
```

A successful result returns typed content blocks:

```json
{"jsonrpc":"2.0","id":5,"result":{
  "content":[{"type":"text","text":"60"}],
  "structuredContent":{"result":"60"},
  "isError":false
}}
```

`content` is a list because a tool can return text, images, audio, embedded
resources, or links. `structuredContent` is optional and is meant for
machine-readable output. `isError: true` means the tool ran but reported a
domain error (division by zero, bad path); it is still a successful JSON-RPC
response. This project formats `isError` results as
`Error from tool '...': ...` so the model can recover.

### 5. Reading a resource

```json
{"jsonrpc":"2.0","id":6,"method":"resources/read","params":{"uri":"notes://index"}}
```

```json
{"jsonrpc":"2.0","id":6,"result":{"contents":[
  {"uri":"notes://index","mimeType":"application/json","text":"[{\"name\":\"react\",...}]"}
]}
```

Resources are identified by URI. A server can expose fixed URIs and URI
templates such as `notes://{name}`; the client fills in the variable and reads
the resulting URI.

### 6. Rendering a prompt

```json
{"jsonrpc":"2.0","id":7,"method":"prompts/get","params":{
  "name":"explain_mcp","arguments":{}
}}
```

```json
{"jsonrpc":"2.0","id":7,"result":{"messages":[
  {"role":"user","content":{"type":"text","text":"Explain the MCP lifecycle..."}}
]}}
```

Prompts are reusable message templates. The host decides when to insert them
into a conversation; the server does not call the model.

### 7. Shutdown

There is no mandatory goodbye request. The client closes stdin (stdio) or the
HTTP session, and the server exits. In this project `Client.__aexit__` and
`RawMCPClient.close()` handle that, including killing a hung subprocess after a
short timeout.

## Message shapes

Every message is a JSON object with `"jsonrpc":"2.0"`.

| Kind | Has `id`? | Has `method`? | Shape |
| --- | --- | --- | --- |
| Request | yes | yes | `{id, method, params}` |
| Success response | yes | no | `{id, result}` |
| Error response | yes | no | `{id, error:{code, message, data?}}` |
| Notification | no | yes | `{method, params?}` |

Standard error codes include `-32700` (parse error), `-32600` (invalid request),
`-32601` (method not found), `-32602` (invalid params), and `-32603` (internal
error). Server-defined codes must stay outside the JSON-RPC reserved range.

## Capabilities in practice

A server advertises only what it implements. This server advertises:

- `tools` - `tools/list`, `tools/call`
- `resources` - `resources/list`, `resources/read`, templates
- `prompts` - `prompts/list`, `prompts/get`
- `logging` - `notifications/message` when the server emits logs

The agent depends only on `tools`. `SDKMCPClient.list_resources()` and
`list_prompts()` are convenience methods for exploration; the ReAct loop never
calls them automatically.

## Where this lives in the code

| Concept | Implementation |
| --- | --- |
| Server construction and transport | `src/react_mcp/server.py` |
| Tool/resource/prompt registration | `src/react_mcp/tools.py` |
| Official-SDK client | `src/react_mcp/mcp_client/sdk_client.py` |
| Raw JSON-RPC client and framing | `src/react_mcp/mcp_client/raw_client.py` |
| Local span/protocol trace | `src/react_mcp/observability/tracer.py` |
| Live frame printer | `src/react_mcp/observability/render.py` |

## Debugging checklist

1. Run `react-mcp doctor` to confirm the model and server.
2. Run `react-mcp protocol-demo` to see exactly which methods are sent and what
   comes back.
3. Look at `protocol.jsonl` for the machine-readable transcript.
4. Look at `spans.jsonl` for timing and nesting, including `mcp.tools/call`.
5. If a tool call is rejected, compare `arguments` with the tool's
   `inputSchema`. The server validates before running your function.

## Security boundaries

- **Validate arguments on the server.** MCP does not provide a trust boundary;
  a malicious host can send anything.
- **Never use `eval`.** This project parses arithmetic with `ast` and allows
  only numeric constants and a fixed operator set.
- **Confine file access.** `_safe_path` resolves symlinks and rejects absolute
  paths, `..`, and anything outside `data/workspace`.
- **Treat resources as data, not instructions.** A note could contain text that
  tries to redirect the agent; keep the system prompt authoritative.
- **Bind HTTP to loopback** unless you add authentication and TLS. The HTTP
  transport here is for local inspection only.
