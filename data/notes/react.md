# The ReAct pattern

ReAct means **Reason + Act**. Instead of asking a model for an answer in one
shot, the loop alternates between a short reasoning trace and an action whose
result is fed back into the context.

The classic text format is:

```text
Thought: I need to evaluate the arithmetic expression.
Action: calculator
Action Input: {"expression": "(17 * 3) + 9"}
```

The runtime executes the tool and appends:

```text
Observation: 60
```

The model then either takes another action or finishes with:

```text
Thought: I now know the final answer.
Final Answer: 60
```

## Why text instead of native tool calling?

Native tool calling hides the decision inside an opaque API field. A text
protocol makes every Thought, Action, and Observation visible in the transcript
and in the trace files, which is the point of this learning project. The trade
off is that the model can emit malformed JSON, so the parser must be forgiving
and the agent needs a retry path.

## Loop mechanics in this project

1. The system prompt lists every MCP tool, its description, and its JSON schema.
2. The model is asked to stop at `Observation:` so it cannot invent tool output.
3. `parse_react_output` extracts a Thought plus either an Action/Action Input or
   a Final Answer. Fenced code, surrounding prose, and single-string inputs are
   tolerated.
4. The agent calls `tools/call` over MCP, formats the observation, and appends
   both the assistant message and a user message containing the observation.
5. The loop ends on a Final Answer, the step limit, an unrecoverable parse
   error, or a forced-final pass that asks the model to answer with what it has.

## Failure modes

- **Unknown tool** - the observation lists the valid names and the model retries.
- **Bad arguments** - missing keys or invalid JSON produce a corrective message.
- **Tool error** - reported as `Error from tool ...`, so the model can recover.
- **Runaway loop** - `REACT_MCP_MAX_STEPS` bounds the work.
- **Wrong answer** - the deterministic tools still return the truth in the
  observation; the model's job is to use it, not to recompute it mentally.
