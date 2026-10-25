"""
Tracing for ClaimSight: one timeline per claim, across both frameworks.

The monitoring log (src/monitoring.py) records what happened on this machine.
This module sends the same run to Langfuse, where a claim appears as a tree:
the Flow, each crew, each agent, each LLM call with its tokens and seconds, and
each tool call. That is what makes a slow or expensive step findable.

Both frameworks report into one trace, because they share OpenTelemetry:

    CrewAI   openinference-instrumentation-crewai  (crews, agents, tasks)
    the LLM  openinference-instrumentation-openai  (calls, tokens, latency)
    ADK      its own OpenTelemetry spans, which land in the provider set here

Tracing is OFF unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set, so
the project runs exactly as before without them.

PRIVACY. A trace carries prompts, so it would carry personal data if any were
left in them. Two things prevent that: documents are already cleaned before any
agent reads them (guardrails.safe_for_cloud), and every span is passed through
redact_pii on the way out. Set LANGFUSE_TRACE_CONTENT=false to send only the
shape of the run - names, timings, tokens - and no text at all.

Set up:
    uv add "langfuse<5" openinference-instrumentation-crewai \\
           openinference-instrumentation-openai \\
           "opentelemetry-sdk==1.44.0" "opentelemetry-api==1.44.0"

    .env:
      LANGFUSE_PUBLIC_KEY=pk-lf-...
      LANGFUSE_SECRET_KEY=sk-lf-...
      LANGFUSE_HOST=https://cloud.langfuse.com     # or your own install
      LANGFUSE_TRACE_CONTENT=true

The OpenTelemetry versions are pinned because google-adk accepts at most 1.44;
a newer opentelemetry-sdk resolves but breaks ADK.
"""

import base64
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

TEXT_ATTRIBUTES = ("input", "output", "prompt", "completion", "message", "content",
                   "value", "query", "document", "description", "arguments", "result")

_started = False
_provider = None


# ---------------------------------------------------------------- scrubbing
def _scrub(value):
    """Mask phone numbers, emails and Aadhaar-like numbers in one attribute."""
    import guardrails
    if isinstance(value, str) and len(value) > 3:
        cleaned, found = guardrails.redact_pii(value)
        return cleaned if found else value
    return value


def scrub_attributes(attributes: dict, send_content: bool = True) -> dict:
    """What a span is allowed to carry out of this machine.

    With send_content off, text is dropped and only the shape of the run is
    kept: names, counts, tokens, seconds. With it on, text goes through the
    same PII redaction the agents' messages do."""
    out = {}
    for key, value in (attributes or {}).items():
        looks_textual = any(word in key.lower() for word in TEXT_ATTRIBUTES)
        if looks_textual and isinstance(value, str):
            if not send_content:
                out[key] = f"[{len(value)} characters not sent]"
                continue
            out[key] = _scrub(value)
        else:
            out[key] = value
    return out


def _scrubbing_exporter(inner, send_content: bool):
    """Wrap an exporter so every span is cleaned on the way out."""
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter

    class Scrubbing(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]):
            cleaned = []
            for span in spans:
                copy = ReadableSpan(
                    name=span.name, context=span.context, parent=span.parent,
                    resource=span.resource,
                    attributes=scrub_attributes(dict(span.attributes or {}), send_content),
                    events=() if not send_content else span.events,
                    links=span.links, kind=span.kind,
                    instrumentation_scope=span.instrumentation_scope,
                    status=span.status, start_time=span.start_time,
                    end_time=span.end_time)
                cleaned.append(copy)
            return inner.export(cleaned)

        def shutdown(self):
            return inner.shutdown()

        def force_flush(self, timeout_millis: int = 30_000):
            return inner.force_flush(timeout_millis)

    return Scrubbing()


# ---------------------------------------------------------------- starting up
def configured() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def start(service: str = "claimsight", exporter=None) -> bool:
    """Turn on tracing. Returns True if it is now on.

    Call this BEFORE importing the crews or ADK: both take their tracer when
    their modules load, so a provider set afterwards would miss their spans.

    `exporter` is for tests, which pass an in-memory one instead of Langfuse."""
    global _started, _provider
    if _started:
        return True
    if exporter is None and not configured():
        return False

    from openinference.instrumentation.crewai import CrewAIInstrumentor
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    send_content = os.getenv("LANGFUSE_TRACE_CONTENT", "true").lower() != "false"
    _provider = TracerProvider(resource=Resource.create({
        "service.name": service,
        "deployment.environment": os.getenv("CLAIMSIGHT_ENV", "development")}))

    if exporter is not None:
        _provider.add_span_processor(
            SimpleSpanProcessor(_scrubbing_exporter(exporter, send_content)))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
        token = base64.b64encode(
            f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}"
            .encode()).decode()
        _provider.add_span_processor(BatchSpanProcessor(_scrubbing_exporter(
            OTLPSpanExporter(endpoint=f"{host}/api/public/otel/v1/traces",
                             headers={"Authorization": f"Basic {token}"}),
            send_content)))

    trace.set_tracer_provider(_provider)
    CrewAIInstrumentor().instrument(tracer_provider=_provider)
    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor
        OpenAIInstrumentor().instrument(tracer_provider=_provider)
    except ImportError:          # tokens and latency simply go untraced
        pass

    _started = True
    return True


def claim_span(claim_id: str, **fields):
    """A span covering one whole claim, so everything below it groups together.

    Used as a context manager:
        with observability.claim_span("CLM-5000", route="auto_approve"):
            ...
    Does nothing when tracing is off, so callers need no condition."""
    from contextlib import nullcontext
    if not _started:
        return nullcontext()
    from opentelemetry import trace
    tracer = trace.get_tracer("claimsight.flow")
    span = tracer.start_as_current_span(f"claim {claim_id}")

    class Scoped:
        def __enter__(self):
            self.span = span.__enter__()
            self.span.set_attribute("claim.id", claim_id)
            for key, value in fields.items():
                if value is not None:
                    self.span.set_attribute(f"claim.{key}", value)
            return self.span

        def __exit__(self, *exc):
            return span.__exit__(*exc)

    return Scoped()


def trace_id() -> Optional[str]:
    """The current trace id, so a claim's local log can point at its trace."""
    if not _started:
        return None
    from opentelemetry import trace
    context = trace.get_current_span().get_span_context()
    if not context or not context.trace_id:
        return None
    return format(context.trace_id, "032x")


def flush() -> None:
    """Send anything still queued. Worth calling before a short script exits."""
    if _provider is not None:
        _provider.force_flush()


def describe() -> str:
    if _started:
        return (f"tracing on -> {os.getenv('LANGFUSE_HOST', 'https://cloud.langfuse.com')}"
                f" (content {'included' if os.getenv('LANGFUSE_TRACE_CONTENT', 'true')
                             .lower() != 'false' else 'withheld'})")
    return "tracing off (set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to turn it on)"
