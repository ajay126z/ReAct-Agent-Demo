"""A small span tracer that writes JSONL and optionally feeds OpenTelemetry.

The tracer is intentionally dependency-light: JSONL files are always produced,
while OTel spans ride along through the API that ``mcp`` already depends on.
Installing the SDK/exporter (``--otel console|otlp``) is what makes them visible.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .logging import get_logger

try:  # opentelemetry-api ships with mcp, but never let tracing break the app
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import Status, StatusCode

    _OTEL_AVAILABLE = True
except Exception:  # pragma: no cover - defensive
    _OTEL_AVAILABLE = False

log = get_logger("tracer")

_CURRENT_SPAN: ContextVar[Span | None] = ContextVar("react_mcp_current_span", default=None)
_CURRENT_OTEL: ContextVar[Any | None] = ContextVar("react_mcp_current_otel", default=None)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def _otel_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in attributes.items():
        if isinstance(value, (str, bool, int, float)):
            cleaned[key] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, bool, int, float)) for item in value
        ):
            cleaned[key] = list(value)
        else:
            cleaned[key] = str(value)
    return cleaned


@dataclass
class Span:
    """One timed unit of work."""

    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    kind: str
    start_ns: int
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    start_time: str = field(default_factory=_utc_now)
    end_time: str | None = None
    end_ns: int | None = None
    status: str = "unset"
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        if self.end_ns is None:
            return (time.perf_counter_ns() - self.start_ns) / 1_000_000
        return (self.end_ns - self.start_ns) / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status,
            "error": self.error,
            "attributes": _jsonable(self.attributes),
            "events": _jsonable(self.events),
        }


class Tracer:
    """Creates spans, writes ``spans.jsonl``/``events.jsonl``/``protocol.jsonl``."""

    def __init__(
        self,
        trace_dir: Path,
        *,
        run_name: str = "run",
        metadata: dict[str, Any] | None = None,
        echo_protocol: bool = False,
    ) -> None:
        self.trace_id = uuid.uuid4().hex
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{stamp}-{self.trace_id[:8]}"
        self.run_dir = trace_dir / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.spans_path = self.run_dir / "spans.jsonl"
        self.events_path = self.run_dir / "events.jsonl"
        self.protocol_path = self.run_dir / "protocol.jsonl"
        self.run_path = self.run_dir / "run.json"
        self.summary_path = self.run_dir / "summary.json"

        self.run_name = run_name
        self.echo_protocol = echo_protocol
        self.spans: list[Span] = []
        self._closed = False
        self._otel_tracer = otel_trace.get_tracer("react_mcp") if _OTEL_AVAILABLE else None

        self._write_json(
            self.run_path,
            {
                "run_id": self.run_id,
                "trace_id": self.trace_id,
                "run_name": run_name,
                "started_at": _utc_now(),
                "metadata": _jsonable(metadata or {}),
            },
        )
        log.debug("trace started run_id=%s dir=%s", self.run_id, self.run_dir)

    # -- spans ------------------------------------------------------------
    @contextmanager
    def span(self, name: str, *, kind: str = "internal", **attributes: Any) -> Iterator[Span]:
        parent = _CURRENT_SPAN.get()
        span = Span(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_id=parent.span_id if parent else None,
            name=name,
            kind=kind,
            start_ns=time.perf_counter_ns(),
            attributes=dict(attributes),
        )

        otel_span = None
        otel_token = None
        if self._otel_tracer is not None:
            otel_span = self._otel_tracer.start_span(
                name,
                kind=self._otel_kind(kind),
                attributes=_otel_attributes(span.attributes),
            )
            otel_token = otel_context.attach(otel_trace.set_span_in_context(otel_span))

        span_token = _CURRENT_SPAN.set(span)
        otel_ctx_token = _CURRENT_OTEL.set(otel_span)
        try:
            yield span
        except Exception as exc:
            span.status = "error"
            span.error = f"{type(exc).__name__}: {exc}"
            if otel_span is not None:
                otel_span.record_exception(exc)
                otel_span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        else:
            if span.status == "unset":
                span.status = "ok"
        finally:
            span.end_ns = time.perf_counter_ns()
            span.end_time = _utc_now()
            self.spans.append(span)
            self._append_jsonl(self.spans_path, span.to_dict())
            if otel_span is not None:
                otel_span.set_attribute("duration_ms", round(span.duration_ms, 3))
                if span.status == "ok":
                    otel_span.set_status(Status(StatusCode.OK))
                otel_span.end()
            _CURRENT_OTEL.reset(otel_ctx_token)
            _CURRENT_SPAN.reset(span_token)
            if otel_token is not None:
                otel_context.detach(otel_token)

    @staticmethod
    def _otel_kind(kind: str) -> Any:
        if not _OTEL_AVAILABLE:
            return None
        mapping = {
            "internal": otel_trace.SpanKind.INTERNAL,
            "client": otel_trace.SpanKind.CLIENT,
            "server": otel_trace.SpanKind.SERVER,
        }
        return mapping.get(kind, otel_trace.SpanKind.INTERNAL)

    def event(self, name: str, **attributes: Any) -> None:
        """Attach a timestamped event to the current span and the event log."""
        current = _CURRENT_SPAN.get()
        record = {
            "trace_id": self.trace_id,
            "span_id": current.span_id if current else None,
            "timestamp": _utc_now(),
            "name": name,
            "attributes": _jsonable(attributes),
        }
        if current is not None:
            current.events.append(record)
        self._append_jsonl(self.events_path, record)

        otel_span = _CURRENT_OTEL.get()
        if otel_span is not None:
            otel_span.add_event(name, attributes=_otel_attributes(attributes))

    # -- protocol frames --------------------------------------------------
    def protocol(
        self, direction: str, message: dict[str, Any], *, echo: bool | None = None
    ) -> None:
        """Record one raw JSON-RPC frame (used by the educational raw client)."""
        record = {
            "timestamp": _utc_now(),
            "direction": direction,
            "message": _jsonable(message),
        }
        self._append_jsonl(self.protocol_path, record)

        should_echo = self.echo_protocol if echo is None else echo
        if should_echo:
            try:
                from .render import print_protocol_frame

                print_protocol_frame(direction, message)
            except Exception:  # pragma: no cover - rendering is best effort
                log.debug("protocol %s %s", direction, message)

    # -- lifecycle --------------------------------------------------------
    def summary(self, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
        by_name: dict[str, int] = {}
        errors = 0
        for span in self.spans:
            by_name[span.name] = by_name.get(span.name, 0) + 1
            if span.status == "error":
                errors += 1
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "run_name": self.run_name,
            "spans": len(self.spans),
            "span_errors": errors,
            "spans_by_name": by_name,
            "metrics": metrics or {},
            "artifacts": {
                "run": str(self.run_path),
                "spans": str(self.spans_path),
                "events": str(self.events_path),
                "protocol": str(self.protocol_path),
            },
        }

    def close(self, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
        summary = self.summary(metrics)
        if not self._closed:
            summary["finished_at"] = _utc_now()
            self._write_json(self.summary_path, summary)
            self._closed = True
        return summary

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
