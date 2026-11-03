"""Ollama chat wrapper with tracing and token accounting."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
from ollama import AsyncClient, ResponseError

from ..config import Settings
from ..observability.logging import get_logger
from ..observability.tracer import Tracer

log = get_logger("llm")


class LLMError(RuntimeError):
    """A model call failed in a way the user can fix."""


@dataclass(slots=True)
class LLMResponse:
    content: str
    thinking: str | None
    prompt_tokens: int
    completion_tokens: int
    duration_ms: float
    done_reason: str | None = None


class OllamaLLM:
    """Async Ollama client for one configured model."""

    def __init__(self, settings: Settings, *, tracer: Tracer | None = None) -> None:
        self.settings = settings
        self.tracer = tracer
        self.client = AsyncClient(host=settings.ollama_host, timeout=settings.request_timeout)

    async def list_models(self) -> list[str]:
        try:
            response = await self.client.list()
        except Exception as exc:  # pragma: no cover - environmental
            raise LLMError(self._explain(exc)) from exc
        models = getattr(response, "models", None) or []
        names: list[str] = []
        for model in models:
            name = getattr(model, "model", None) or getattr(model, "name", None)
            if name:
                names.append(str(name))
        return names

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        stop: list[str] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        options: dict[str, Any] = {
            "temperature": self.settings.temperature if temperature is None else temperature,
            "num_ctx": self.settings.num_ctx,
        }
        if stop:
            options["stop"] = stop

        attributes = {
            "gen_ai.system": "ollama",
            "gen_ai.request.model": self.settings.model,
            "message_count": len(messages),
            "prompt_chars": sum(len(message.get("content", "")) for message in messages),
            "temperature": options["temperature"],
            "num_ctx": options["num_ctx"],
            "think": self.settings.think,
        }

        def _call() -> Any:
            return self._chat_call(messages, options)

        if self.tracer is not None:
            with self.tracer.span("llm.chat", kind="client", **attributes) as span:
                response, duration_ms = await self._run(_call())
                result = self._to_response(response, duration_ms)
                span.attributes.update(
                    {
                        "gen_ai.usage.input_tokens": result.prompt_tokens,
                        "gen_ai.usage.output_tokens": result.completion_tokens,
                        "duration_ms": round(result.duration_ms, 2),
                        "response_chars": len(result.content),
                    }
                )
                self.tracer.event(
                    "llm.response",
                    model=self.settings.model,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    done_reason=result.done_reason,
                )
        else:
            response, duration_ms = await self._run(_call())
            result = self._to_response(response, duration_ms)

        log.debug(
            "llm response model=%s prompt_tokens=%s completion_tokens=%s duration_ms=%.0f",
            self.settings.model,
            result.prompt_tokens,
            result.completion_tokens,
            result.duration_ms,
        )
        return result

    def _chat_call(self, messages: list[dict[str, str]], options: dict[str, Any]) -> Any:
        return self.client.chat(
            model=self.settings.model,
            messages=messages,
            stream=False,
            think=self.settings.think,
            options=options,
            keep_alive=self.settings.keep_alive,
        )

    async def _run(self, awaitable: Any) -> tuple[Any, float]:
        started = time.perf_counter()
        try:
            response = await awaitable
        except Exception as exc:
            raise LLMError(self._explain(exc)) from exc
        return response, (time.perf_counter() - started) * 1000

    def _to_response(self, response: Any, duration_ms: float) -> LLMResponse:
        message = getattr(response, "message", None)
        content = (getattr(message, "content", None) or "").strip()
        thinking = getattr(message, "thinking", None)
        if not content and thinking:
            log.warning("model returned empty content; falling back to its thinking text")
            content = str(thinking).strip()
        return LLMResponse(
            content=content,
            thinking=thinking,
            prompt_tokens=int(getattr(response, "prompt_eval_count", 0) or 0),
            completion_tokens=int(getattr(response, "eval_count", 0) or 0),
            duration_ms=duration_ms,
            done_reason=getattr(response, "done_reason", None),
        )

    def _explain(self, exc: Exception) -> str:
        host = self.settings.ollama_host
        model = self.settings.model
        if isinstance(exc, ResponseError):
            text = str(exc)
            if "not found" in text.lower():
                return (
                    f"Ollama could not find model {model!r}. Run `ollama list` and "
                    f"`ollama pull {model}`, or pass --model with an installed model."
                )
            return f"Ollama returned an error: {text}"
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return (
                f"Could not connect to Ollama at {host}. Start it with `ollama serve` "
                "or set REACT_MCP_OLLAMA_HOST."
            )
        if isinstance(exc, httpx.ReadTimeout):
            return (
                f"Ollama timed out after {self.settings.request_timeout}s. "
                "Try a smaller model, a smaller --num-ctx, or a larger timeout."
            )
        return f"Ollama call failed: {type(exc).__name__}: {exc}"
