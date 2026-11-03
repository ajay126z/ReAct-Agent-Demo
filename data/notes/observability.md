# Observability: traces, spans, and metrics

An agent is a distributed system in miniature: the LLM call, the MCP client,
the MCP server subprocess, and the tools all participate in one user request.
Observability means being able to reconstruct that request after the fact.

## What this project records

Each CLI run gets a directory under `traces/<timestamp>-<run-id>/`:

- `run.json` - run id, start time, and environment metadata.
- `spans.jsonl` - one JSON object per finished span.
- `events.jsonl` - standalone timestamped events, also attached to their span.
- `protocol.jsonl` - raw MCP JSON-RPC frames, when protocol echo is enabled.
- `summary.json` - span counts, final metrics, and artifact paths.

Spans are nested. A typical `ask` run looks like:

```text
agent.run
  agent.step
    llm.chat
    mcp.tools/call
  agent.finish
```

Each span carries attributes such as the model, tool name, arguments, token
counts, or parse result. Durations are measured with a monotonic clock.

## Metrics

The agent counts steps, LLM calls, prompt/completion tokens, tool calls, tool
errors, parse retries, and wall-clock time. Token counts come from Ollama; they
are estimates of model work, not billing units, because the model runs locally.

## Optional OpenTelemetry

The tracer can also emit spans to an OTel exporter. Install the extra with
`uv sync --all-extras`, then run with `--otel console` or `--otel otlp`. The
local JSONL trace is always the source of truth; OTel is for connecting this
toy to a real backend such as Jaeger, Tempo, or Honeycomb.

## Inspecting a run

```bash
ls traces
jq -c '{name, ms:.duration_ms, status}' traces/*/spans.jsonl
```

Because traces are plain JSONL, they are easy to diff, grep, and feed into a
notebook. Nothing is uploaded anywhere.
