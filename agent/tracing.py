"""OpenTelemetry setup for the agent. Every LLM call and every tool call
gets a span with truncated inputs/outputs, token counts, and latency
(automatic from span duration).

Exports to JSON files by default (TRACE_EXPORTER=json) rather than Jaeger:
this project already pushes an 8-core/16GB Mac hard running a full kind
cluster plus local LLM inference simultaneously (see docs/DECISIONS.md,
Phase 4 - CPU spiked to 1700-2000%+ under that combination); adding an
always-on Jaeger container for a feature that's purely for human
inspection after the fact isn't worth the extra resource pressure. The
spec explicitly allows this fallback ("Jaeger all-in-one, or JSON trace
files plus a viewer page if too heavy"). Set TRACE_EXPORTER=otlp (with
OTEL_EXPORTER_OTLP_ENDPOINT pointed at a real Jaeger/Tempo/collector) to
switch without touching any instrumentation code - that's the point of
going through the real OTel SDK instead of a bespoke logger.
"""
import json
import os
import threading
from pathlib import Path

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

ROOT = Path(__file__).resolve().parent.parent
TRACES_DIR = ROOT / "traces"
TRACES_DIR.mkdir(exist_ok=True)
SPANS_PATH = TRACES_DIR / "spans.jsonl"

TRACE_EXPORTER = os.environ.get("TRACE_EXPORTER", "json")
MAX_ATTR_CHARS = int(os.environ.get("TRACE_MAX_ATTR_CHARS", "500"))

_lock = threading.Lock()


def truncate(value, max_chars: int = MAX_ATTR_CHARS) -> str:
    """Convert any value to a string safe to attach as a span attribute,
    cutting it off (with a marker showing the original length) past
    max_chars. Used on every LLM input/output preview and tool
    argument/result so one verbose call can't bloat spans.jsonl or make the
    trace viewer unusable - non-string values are JSON-encoded first so
    dicts/lists get a readable preview too.
    """
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... (truncated, {len(text)} chars total)"


class JSONFileSpanExporter(SpanExporter):
    """Appends each finished span as one JSON line to traces/spans.jsonl.
    Shared across all incidents handled by this process - the viewer
    groups spans by trace_id, one trace per investigation."""

    def export(self, spans) -> SpanExportResult:
        # A lock guards the append since SimpleSpanProcessor can call
        # export() from whatever thread ends a span - without it, concurrent
        # writes could interleave and corrupt a line.
        with _lock, SPANS_PATH.open("a") as f:
            for span in spans:
                ctx = span.get_span_context()
                parent = span.parent
                # IDs are stored as fixed-width hex strings (the OTel wire
                # format) so the viewer can match parent_span_id to span_id
                # across lines without worrying about int vs hex mismatches.
                record = {
                    "trace_id": format(ctx.trace_id, "032x"),
                    "span_id": format(ctx.span_id, "016x"),
                    "parent_span_id": format(parent.span_id, "016x") if parent else None,
                    "name": span.name,
                    "start_time_ns": span.start_time,
                    "end_time_ns": span.end_time,
                    # duration_ms is derived here, once, rather than making
                    # the viewer redo the ns-to-ms math per span.
                    "duration_ms": round((span.end_time - span.start_time) / 1e6, 2) if span.end_time else None,
                    "status": span.status.status_code.name,
                    "attributes": dict(span.attributes or {}),
                }
                f.write(json.dumps(record, default=str) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self):
        # Required by the SpanExporter interface; nothing to flush/close
        # since every export() call already opens, writes, and closes the
        # file immediately.
        pass


_provider = None


def get_tracer():
    # Deliberately calls provider.get_tracer(...) on our own tracked
    # TracerProvider instead of the global trace.get_tracer() convenience
    # function: OTel's global registration (trace.set_tracer_provider) is
    # a one-shot operation that silently refuses to override itself, which
    # makes the global API unusable for tests that need a fresh provider
    # per test (e.g. backed by InMemorySpanExporter). Managing our own
    # provider reference sidesteps that entirely.
    global _provider
    if _provider is None:
        _provider = TracerProvider(resource=Resource.create({"service.name": "incident-agent"}))
        if TRACE_EXPORTER == "otlp":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter()
        else:
            exporter = JSONFileSpanExporter()
        _provider.add_span_processor(SimpleSpanProcessor(exporter))
    return _provider.get_tracer("incident-agent")


def reset_tracer_for_testing(exporter: SpanExporter):
    """Swap in a fresh TracerProvider backed by the given exporter (e.g.
    InMemorySpanExporter) - lets tests assert on spans without writing to
    traces/spans.jsonl or needing TRACE_EXPORTER configured."""
    global _provider
    _provider = TracerProvider(resource=Resource.create({"service.name": "incident-agent-test"}))
    _provider.add_span_processor(SimpleSpanProcessor(exporter))
