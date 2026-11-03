"""The ReAct loop: reason, call an MCP tool, observe, repeat."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..mcp_client.base import MCPToolClient, ToolSpec
from ..observability.logging import get_logger
from ..observability.metrics import RunMetrics
from ..observability.tracer import Span, Tracer
from .llm import OllamaLLM
from .parser import ParseResult, parse_react_output
from .prompts import FORCE_FINAL_PROMPT, PARSE_RETRY_TEMPLATE, build_system_prompt

log = get_logger("agent")


@dataclass
class AgentResult:
    answer: str
    steps: int
    metrics: RunMetrics
    transcript: list[dict[str, Any]] = field(default_factory=list)
    stopped_reason: str = "final_answer"
    model: str = ""


class ReActAgent:
    """Text-based ReAct agent whose tools come from an MCP server."""

    def __init__(
        self,
        *,
        llm: OllamaLLM,
        client: MCPToolClient,
        tools: list[ToolSpec],
        settings: Settings,
        tracer: Tracer | None = None,
        show_steps: bool = True,
    ) -> None:
        self.llm = llm
        self.client = client
        self.tools = tools
        self.tool_map = {tool.name: tool for tool in tools}
        self.settings = settings
        self.tracer = tracer
        self.show_steps = show_steps

    @contextmanager
    def _span(self, name: str, **attributes: Any) -> Iterator[Span | None]:
        if self.tracer is None:
            with nullcontext(None) as span:
                yield span
        else:
            with self.tracer.span(name, **attributes) as span:
                yield span

    async def run(self, question: str) -> AgentResult:
        metrics = RunMetrics()
        transcript: list[dict[str, Any]] = []
        messages: list[dict[str, str]] = [
            {"role": "system", "content": build_system_prompt(self.tools)},
            {"role": "user", "content": question},
        ]

        answer = ""
        stopped_reason = "max_steps"
        parse_retries = 0
        last_text = ""

        if self.show_steps:
            from ..observability.render import print_question

            print_question(question)

        with self._span(
            "agent.run",
            question=question,
            model=self.settings.model,
            max_steps=self.settings.max_steps,
        ) as run_span:
            for step in range(1, self.settings.max_steps + 1):
                metrics.steps = step
                with self._span("agent.step", step=step) as step_span:
                    response = await self.llm.chat(messages, stop=["Observation:"])
                    metrics.record_llm(
                        prompt_tokens=response.prompt_tokens,
                        completion_tokens=response.completion_tokens,
                        duration_ms=response.duration_ms,
                    )
                    text = response.content.strip()
                    last_text = text
                    parsed = parse_react_output(text)
                    transcript.append(
                        {
                            "step": step,
                            "kind": parsed.kind,
                            "thought": parsed.thought,
                            "raw_output": text,
                        }
                    )
                    if step_span is not None:
                        step_span.attributes["parse_kind"] = parsed.kind

                    if parsed.kind == "final":
                        answer = parsed.final_answer or ""
                        stopped_reason = "final_answer"
                        if self.show_steps:
                            from ..observability.render import print_step

                            print_step(step=step, thought=parsed.thought, raw_output=None)
                        break

                    if parsed.kind == "invalid":
                        metrics.parse_retries += 1
                        if self.show_steps:
                            from ..observability.render import print_step

                            print_step(step=step, thought=parsed.thought, raw_output=text)
                        if parse_retries >= self.settings.max_parse_retries:
                            stopped_reason = "parse_error"
                            break
                        parse_retries += 1
                        messages.append({"role": "assistant", "content": text or "(empty)"})
                        messages.append(
                            {
                                "role": "user",
                                "content": PARSE_RETRY_TEMPLATE.format(
                                    error=parsed.error or "unknown parse error"
                                ),
                            }
                        )
                        continue

                    execution = await self._execute_action(parsed, metrics)
                    transcript[-1].update(
                        {
                            "action": execution["action"],
                            "arguments": execution["arguments"],
                            "observation": execution["observation"],
                            "is_error": execution["is_error"],
                        }
                    )
                    if self.show_steps:
                        from ..observability.render import print_observation, print_step

                        print_step(
                            step=step,
                            thought=parsed.thought,
                            action=execution["action"],
                            arguments=execution["arguments"],
                        )
                        print_observation(execution["observation"], is_error=execution["is_error"])

                    messages.append({"role": "assistant", "content": text})
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Observation: {execution['observation']}",
                        }
                    )

                    if step_span is not None:
                        step_span.attributes["tool"] = execution["action"]

            if not answer:
                answer, stopped_reason = await self._force_final(
                    messages, metrics, stopped_reason, last_text
                )

            metrics.finish()
            if run_span is not None:
                run_span.attributes.update(
                    {
                        "steps": metrics.steps,
                        "stop_reason": stopped_reason,
                        "answer_chars": len(answer),
                        **metrics.as_dict(),
                    }
                )
            if self.tracer is not None:
                self.tracer.event(
                    "agent.finish",
                    stop_reason=stopped_reason,
                    answer_chars=len(answer),
                    **metrics.as_dict(),
                )

        if self.show_steps:
            from ..observability.render import print_final

            print_final(answer, steps=metrics.steps)

        return AgentResult(
            answer=answer,
            steps=metrics.steps,
            metrics=metrics,
            transcript=transcript,
            stopped_reason=stopped_reason,
            model=self.settings.model,
        )

    async def _execute_action(self, parsed: ParseResult, metrics: RunMetrics) -> dict[str, Any]:
        action = parsed.action or ""
        spec = self.tool_map.get(action)
        if spec is None:
            metrics.tool_errors += 1
            names = ", ".join(sorted(self.tool_map)) or "none"
            observation = (
                f"Error: unknown tool {action!r}. Available tools: {names}. "
                "Choose one of those names."
            )
            if self.tracer is not None:
                self.tracer.event("agent.unknown_tool", tool=action)
            return {
                "action": action,
                "arguments": parsed.arguments,
                "observation": observation,
                "is_error": True,
            }

        arguments, argument_error = self._prepare_arguments(spec, parsed)
        if argument_error is not None:
            metrics.tool_errors += 1
            observation = f"Error: {argument_error}"
            if self.tracer is not None:
                self.tracer.event("agent.bad_arguments", tool=action, error=argument_error)
            return {
                "action": action,
                "arguments": parsed.raw_input or parsed.arguments,
                "observation": observation,
                "is_error": True,
            }

        started = time.perf_counter()
        try:
            result = await self.client.call_tool(action, arguments or {})
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            metrics.record_tool(duration_ms=duration_ms, is_error=True)
            observation = f"Error: tool {action!r} failed: {type(exc).__name__}: {exc}"
            log.warning("tool call failed tool=%s error=%s", action, exc)
            return {
                "action": action,
                "arguments": arguments,
                "observation": observation,
                "is_error": True,
            }

        duration_ms = (time.perf_counter() - started) * 1000
        metrics.record_tool(duration_ms=duration_ms, is_error=result.is_error)
        observation = result.as_observation(limit=self.settings.max_observation_chars)
        if result.is_error:
            observation = f"Error from tool {action!r}: {observation}"
        return {
            "action": action,
            "arguments": arguments,
            "observation": observation,
            "is_error": result.is_error,
        }

    @staticmethod
    def _prepare_arguments(
        spec: ToolSpec, parsed: ParseResult
    ) -> tuple[dict[str, Any] | None, str | None]:
        value = parsed.arguments
        properties = spec.properties

        if value is not None:
            if isinstance(value, dict):
                return value, None
            if len(properties) == 1:
                return {next(iter(properties)): value}, None
            return None, (
                f"Action Input for tool {spec.name!r} must be a JSON object, "
                f"not {type(value).__name__}."
            )

        raw = (parsed.raw_input or "").strip()
        if not raw:
            return {}, None
        if len(properties) == 1:
            if raw[:1] in "{[":
                return None, parsed.error or "Action Input is not valid JSON."
            return {next(iter(properties)): raw}, None
        return None, (
            f"Action Input for tool {spec.name!r} must be a JSON object with keys: "
            f"{', '.join(properties) or '(none)'}."
        )

    async def _force_final(
        self,
        messages: list[dict[str, str]],
        metrics: RunMetrics,
        stopped_reason: str,
        last_text: str,
    ) -> tuple[str, str]:
        log.debug("forcing a final answer after %s", stopped_reason)
        messages.append({"role": "user", "content": FORCE_FINAL_PROMPT})
        with self._span("agent.force_final", reason=stopped_reason):
            response = await self.llm.chat(messages, stop=["Observation:"])
            metrics.record_llm(
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                duration_ms=response.duration_ms,
            )
        parsed = parse_react_output(response.content)
        if parsed.kind == "final" and parsed.final_answer:
            return parsed.final_answer, "forced_final"
        if response.content.strip():
            return response.content.strip(), "unparsed_final"
        return last_text or "I could not produce an answer within the step limit.", "step_limit"
