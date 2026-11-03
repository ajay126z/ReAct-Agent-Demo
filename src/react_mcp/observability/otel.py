"""Optional OpenTelemetry SDK/exporter setup.

The MCP SDK already emits spans through the OpenTelemetry API. Installing the
``otel`` extra and passing ``--otel console`` (or ``--otel otlp``) makes both the
MCP spans and this project's agent spans visible.
"""

from __future__ import annotations

import sys

from .logging import get_logger

log = get_logger("otel")

_PROVIDER = None


def configure_otel(mode: str, *, service_name: str = "react-mcp") -> bool:
    """Configure a global tracer provider. Returns True when tracing is active."""
    global _PROVIDER

    if mode == "none":
        return False
    if _PROVIDER is not None:
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "OpenTelemetry extra is not installed. Run `uv sync --all-extras` "
            "or `uv add 'react-mcp[otel]'`."
        ) from exc

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))

    if mode == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
    elif mode == "otlp":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        from ..config import Settings

        endpoint = Settings().otlp_endpoint
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    else:  # pragma: no cover - guarded by settings literal
        raise ValueError(f"Unknown OTel mode: {mode!r}")

    trace.set_tracer_provider(provider)
    _PROVIDER = provider
    log.info("OpenTelemetry enabled (mode=%s, service=%s)", mode, service_name)
    return True


def shutdown_otel() -> None:
    """Flush and stop the provider, if one was configured."""
    global _PROVIDER
    if _PROVIDER is not None:
        try:
            _PROVIDER.shutdown()
        except Exception:  # pragma: no cover - best effort
            log.debug("OpenTelemetry shutdown failed", exc_info=True)
        _PROVIDER = None
