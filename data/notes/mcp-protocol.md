# MCP protocol in one page

The Model Context Protocol (MCP) is a JSON-RPC 2.0 protocol between a **host**
(the application), a **client** (one connection to one server), and a **server**
(the process that exposes capabilities).

## Transports

- **stdio** - the client launches the server as a subprocess and exchanges
  newline-delimited JSON on stdin/stdout. stderr is free for logs. This project
  uses stdio by default, which is why the client launches
  `python -m react_mcp.server`.
- **Streamable HTTP** - the server listens on an HTTP endpoint and the client
  POSTs JSON-RPC messages. Run `react-mcp serve --http` to try it.

## Lifecycle

1. **Initialize** - the client sends `initialize` with its protocol version,
   capabilities, and client info. The server replies with its own protocol
   version, capabilities, and server info. Both sides must agree on a version.
2. **Initialized notification** - the client sends `notifications/initialized`
   to say the handshake is complete.
3. **Discovery** - `tools/list`, `resources/list`, and `prompts/list` enumerate
   what the server offers.
4. **Use** - `tools/call` invokes a tool; `resources/read` fetches a resource;
   `prompts/get` renders a prompt template.
5. **Shutdown** - the client closes the transport and the server exits.

Newer protocol revisions also support a `server/discover` request, which lets a
client learn the server's capabilities without the older initialize handshake.
This project's official-SDK client negotiates automatically; the raw client in
`mcp_client/raw_client.py` deliberately performs the legacy `initialize` version
(protocol revision `2025-11-25`) so the frames are easy to study.

## A tools/call request

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call",
 "params":{"name":"calculator","arguments":{"expression":"(17 * 3) + 9"}}}
```

The result contains a `content` array of typed blocks. This server returns a
single text block, plus `structuredContent` when the tool returns JSON data.
An `isError` flag marks tool-level failures; protocol-level failures use a
JSON-RPC `error` object instead.

## Capabilities are opt-in

A server only advertises what it implements. This server advertises tools,
resources, prompts, and logging. A client should check the capability flags
before calling the corresponding methods.

## Security notes

- Tool arguments are untrusted input. Validate them on the server, and never
  `eval` them.
- Bind HTTP servers to `127.0.0.1` unless you add authentication and TLS.
- The client controls which server command it launches; do not run an MCP
  server from an untrusted source.
