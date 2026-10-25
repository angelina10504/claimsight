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
                                                   # (LANGFUSE_BASE_URL is read too)
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
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

# Read .env here, and before anything else in this module runs. The entry
# points call start() as their first action, which is right - both frameworks
# bind their tracer when they load - but it meant start() ran before any module
# that reads .env, so the Langfuse keys looked absent and tracing silently
# stayed off.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:              # python-dotenv is optional
    pass

# Regional hosts. An unset LANGFUSE_HOST is not wrong: the EU host is the
# default. But a key issued for one region is rejected by the other with a
# 401, which is the likeliest cause of an unexplained 401.
DEFAULT_HOST = "https://cloud.langfuse.com"
KNOWN_HOSTS = (DEFAULT_HOST, "https://us.cloud.langfuse.com")

TEXT_ATTRIBUTES = ("input", "output", "prompt", "completion", "message", "content",
                   "value", "query", "document", "description", "arguments", "result",
                   "body", "text", "answer", "reason", "summary", "narrative")

_started = False
_provider = None
_switch = None          # the one processor, whose exporter start() can replace
_memory = None          # set when spans are kept in memory instead of sent


# ---------------------------------------------------------------- scrubbing
def _scrub(value):
    """Mask phone numbers, emails and Aadhaar-like numbers in one attribute."""
    import guardrails
    if isinstance(value, str) and len(value) > 3:
        cleaned, found = guardrails.redact_pii(value)
        return cleaned if found else value
    return value


def _scrub_nested(value, send_content: bool):
    """Clean a value that is a list or tuple of strings, as several
    instrumentations report messages."""
    if isinstance(value, (list, tuple)):
        if not send_content:
            return [f"[{len(value)} item(s) not sent]"]
        return [_scrub(v) if isinstance(v, str) else v for v in value]
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
            out[key] = (f"[{len(value)} characters not sent]" if not send_content
                        else _scrub(value))
        elif looks_textual and isinstance(value, (list, tuple)):
            out[key] = _scrub_nested(value, send_content)
        elif isinstance(value, str) and not send_content and len(value) > 200:
            # a long string under an unremarkable key is still content
            out[key] = f"[{len(value)} characters not sent]"
        else:
            out[key] = value
    return out


def _scrub_events(events, send_content: bool):
    """Span events carry content of their own: an exception message, a logged
    prompt. They go through the same cleaning, and are dropped entirely when
    content is withheld."""
    if not events:
        return ()
    if not send_content:
        return ()
    from opentelemetry.sdk.trace import Event
    cleaned = []
    for event in events:
        cleaned.append(Event(
            name=event.name,
            attributes=scrub_attributes(dict(event.attributes or {}), send_content),
            timestamp=event.timestamp))
    return tuple(cleaned)


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
                    events=_scrub_events(span.events, send_content),
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


def _switchable(inner):
    """One processor for the life of the process, whose exporter can change.

    OpenTelemetry lets the global provider be set only once, so a later
    start(exporter=...) cannot install a new provider; it would silently keep
    the old one and its exporter. It replaces the processor behind this one."""
    from opentelemetry.sdk.trace import SpanProcessor

    class Switchable(SpanProcessor):
        def __init__(self):
            self.inner = inner

        def replace(self, new):
            old, self.inner = self.inner, new
            old.shutdown()               # sends what the old one still held

        def on_start(self, span, parent_context=None):
            self.inner.on_start(span, parent_context=parent_context)

        def on_end(self, span):
            self.inner.on_end(span)

        def shutdown(self):
            self.inner.shutdown()

        def force_flush(self, timeout_millis: int = 30_000):
            return self.inner.force_flush(timeout_millis)

    return Switchable()


def _processor_for(exporter):
    """A processor around `exporter`, or around Langfuse when it is None.
    Every span goes through the scrubbing exporter either way."""
    global _memory
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    content = send_content()
    if exporter is not None:
        _memory = exporter if hasattr(exporter, "get_finished_spans") else None
        return SimpleSpanProcessor(_scrubbing_exporter(exporter, content))
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    _memory = None
    return BatchSpanProcessor(_scrubbing_exporter(
        OTLPSpanExporter(endpoint=endpoint(), headers=auth_header()), content))


# ---------------------------------------------------------------- starting up
def configured() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def offline() -> bool:
    """Is remote export forbidden in this process?

    Offline tests set CLAIMSIGHT_OFFLINE. Without it, a test that happens to
    start the A2A server would pick up the real keys from .env and post spans
    to Langfuse, which is how an automated test came to report a 401."""
    return os.getenv("CLAIMSIGHT_OFFLINE", "").lower() in {"1", "true", "yes"}


def send_content() -> bool:
    """May span text leave the machine? Withheld unless asked for: a trace is
    a copy of every prompt, so the default has to be the safe one, not the
    useful one."""
    return os.getenv("LANGFUSE_TRACE_CONTENT", "false").lower() == "true"


def host_setting() -> Optional[str]:
    """The variable the host was taken from, or None for the default.

    LANGFUSE_HOST comes first. LANGFUSE_BASE_URL is the name the Langfuse SDK
    itself documents, so a .env written from its docs sets that one instead;
    ignoring it sent EU traffic for a US key, which is answered with a 401."""
    for name in ("LANGFUSE_HOST", "LANGFUSE_BASE_URL"):
        if os.getenv(name):
            return name
    return None


def host() -> str:
    name = host_setting()
    return (os.getenv(name) if name else DEFAULT_HOST).rstrip("/")


def endpoint(for_host: Optional[str] = None) -> str:
    return f"{(for_host or host()).rstrip('/')}/api/public/otel/v1/traces"


def auth_header() -> dict:
    token = base64.b64encode(
        f"{os.getenv('LANGFUSE_PUBLIC_KEY', '')}:"
        f"{os.getenv('LANGFUSE_SECRET_KEY', '')}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def diagnose() -> dict:
    """What is configured, with nothing secret in it.

    Keys are described by their prefix and length only: enough to tell a
    public key from a secret key, or a truncated paste from a whole one,
    without putting a credential in a terminal or a screenshot."""
    def describe_key(name: str) -> str:
        value = os.getenv(name) or ""
        if not value:
            return "not set"
        prefix = value[:6]
        return f"set, starts {prefix}..., {len(value)} characters"

    return {
        "public_key": describe_key("LANGFUSE_PUBLIC_KEY"),
        "secret_key": describe_key("LANGFUSE_SECRET_KEY"),
        "host": host(),
        "host_explicitly_set": host_setting() is not None,
        "host_from": host_setting() or "default",
        "endpoint": endpoint(),
        "send_content": send_content(),
        "offline": offline(),
        "started": _started,
    }


def start(service: str = "claimsight", exporter=None) -> bool:
    """Turn on tracing. Returns True if it is now on.

    Call this BEFORE importing the crews or ADK: both take their tracer when
    their modules load, so a provider set afterwards would miss their spans.

    `exporter` is for tests, which pass an in-memory one instead of Langfuse.
    It is always installed: if tracing is already on, it replaces whatever
    exporter was in use, so the result does not depend on what ran before."""
    global _started, _provider, _switch
    if _started:
        if exporter is not None:
            _switch.replace(_processor_for(exporter))
        return True
    if os.getenv("CLAIMSIGHT_TRACING", "").lower() == "off":
        return False
    if exporter is None and offline():
        # Tracing on, nothing leaving the machine: spans are collected in
        # memory so the instrumentation is still exercised.
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
        exporter = InMemorySpanExporter()
    if exporter is None and not configured():
        return False

    from openinference.instrumentation.crewai import CrewAIInstrumentor
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    _provider = TracerProvider(resource=Resource.create({
        "service.name": service,
        "deployment.environment": os.getenv("CLAIMSIGHT_ENV", "development")}))
    _switch = _switchable(_processor_for(exporter))
    _provider.add_span_processor(_switch)

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


def collected() -> list:
    """Spans held in memory, when running offline. Empty otherwise."""
    if _memory is None:
        return []
    return list(_memory.get_finished_spans())


def describe() -> str:
    content = "content included" if send_content() else "content withheld"
    if not _started:
        return ("tracing off (set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY "
                "to turn it on)")
    if _memory is not None:
        return f"tracing on, in memory only, nothing sent ({content})"
    return f"tracing on -> {host()} ({content})"
