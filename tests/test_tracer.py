from __future__ import annotations

import json
from pathlib import Path

import pytest

from react_mcp.observability.tracer import Tracer


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_nested_spans_events_and_summary(settings) -> None:
    tracer = Tracer(settings.trace_dir, run_name="test", metadata={"model": "test-model"})
    with tracer.span("outer") as outer:
        tracer.event("outer.event", value=1)
        with tracer.span("inner", kind="client", foo="bar"):
            tracer.event("inner.event")
    tracer.protocol("send", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, echo=False)
    summary = tracer.close({"steps": 1})

    assert summary["spans"] == 2
    assert summary["span_errors"] == 0
    assert summary["metrics"] == {"steps": 1}

    spans = {span["name"]: span for span in read_jsonl(tracer.spans_path)}
    assert spans["outer"]["parent_id"] is None
    assert spans["inner"]["parent_id"] == outer.span_id
    assert spans["inner"]["attributes"]["foo"] == "bar"
    assert spans["inner"]["kind"] == "client"
    assert spans["outer"]["status"] == "ok"
    assert spans["inner"]["duration_ms"] >= 0

    events = read_jsonl(tracer.events_path)
    names = [event["name"] for event in events]
    assert "outer.event" in names
    assert "inner.event" in names

    frames = read_jsonl(tracer.protocol_path)
    assert frames[0]["direction"] == "send"
    assert frames[0]["message"]["method"] == "ping"

    run = json.loads(tracer.run_path.read_text(encoding="utf-8"))
    assert run["metadata"]["model"] == "test-model"
    assert tracer.summary_path.is_file()


def test_error_span_is_marked(settings) -> None:
    tracer = Tracer(settings.trace_dir, run_name="error")
    with pytest.raises(RuntimeError, match="boom"), tracer.span("bad"):
        raise RuntimeError("boom")
    summary = tracer.close()

    assert summary["span_errors"] == 1
    spans = read_jsonl(tracer.spans_path)
    assert spans[0]["status"] == "error"
    assert "boom" in spans[0]["error"]


def test_event_outside_span_has_no_span_id(settings) -> None:
    tracer = Tracer(settings.trace_dir, run_name="events")
    tracer.event("standalone", answer=42)
    tracer.close()

    events = read_jsonl(tracer.events_path)
    assert events[0]["span_id"] is None
    assert events[0]["attributes"]["answer"] == 42
