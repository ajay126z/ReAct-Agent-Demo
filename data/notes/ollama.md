# Running models locally with Ollama

Ollama serves open-weight models over a small HTTP API. The Python package
`ollama` wraps that API with an `AsyncClient`.

## Useful commands

```bash
ollama list
ollama pull qwen3.5:9b
ollama run qwen3.5:9b
ollama ps
```

The API lives at `http://localhost:11434` by default. Point the project at a
different host with `REACT_MCP_OLLAMA_HOST`.

## Chat requests

This project sends a `messages` list with `system`, `user`, and `assistant`
roles. Two options matter most:

- `temperature` - lower values make tool arguments more repeatable. The project
  defaults to `0.2`.
- `num_ctx` - the context window. Tool schemas, the transcript, and observations
  all consume it, so long sessions eventually need a larger value.

Other knobs include `num_predict` (maximum generated tokens), `top_p`, and
`keep_alive` (how long the model stays loaded). The client sets `keep_alive` so
repeated questions in the REPL do not reload the model each time.

Some newer models can emit a separate `thinking` channel. It is disabled by
default here because it adds latency and tokens; enable it with `--think`.

## Tokens and timing

The response reports `prompt_eval_count`, `eval_count`, and duration fields in
nanoseconds. The agent records prompt/completion tokens and per-call latency in
its run metrics so you can see where time goes.

## Troubleshooting

- `Connection refused` - Ollama is not running; start `ollama serve`.
- `model not found` - run `ollama pull <model>` or pick a model from
  `ollama list`.
- Very slow first call - the model is loading into memory. Later calls reuse it
  while `keep_alive` holds it.
- Context overflow - raise `REACT_MCP_NUM_CTX` or lower the step limit.
- Nonsense tool syntax - try a larger model or a lower temperature; the parser
  retries once or twice before giving up.
