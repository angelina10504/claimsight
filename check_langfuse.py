"""
Check that Langfuse will actually accept a trace from this machine.

Nothing else in the project talks to Langfuse's servers: every automated test
keeps its spans in memory. This script is the one place that makes a real
request, and you run it on purpose.

It sends ONE span containing nothing but synthetic metadata: a name, a
timestamp and a marker saying it is a connectivity check. No claim, no
prompt, no model is involved, so it costs no LLM tokens and reveals nothing
about any claimant.

It never prints a key. Credentials are described by their first few characters
and their length, which is enough to spot a key pasted into the wrong field or
truncated, and safe to put in a screenshot.

Run:  uv run python check_langfuse.py
"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

os.environ["CLAIMSIGHT_OFFLINE"] = ""        # this script is the exception

import observability   # noqa: E402

# A POST the server will reject on its content but must first authenticate.
# A 400 therefore proves the credentials were accepted, which is exactly what
# needs establishing, without sending a real payload.
PROBE_BODY = b""
MEANING = {
    400: "the credentials were accepted (the empty test body was rejected, as expected)",
    401: "the credentials were refused: wrong key, or a key from the other region",
    403: "the credentials were understood but not allowed to write here",
    404: "no OpenTelemetry endpoint at that address: check LANGFUSE_HOST",
    405: "the address exists but does not take a POST: check LANGFUSE_HOST",
}


def probe(host: str) -> tuple:
    """Try to authenticate against one host. Returns (status code, meaning)."""
    import httpx
    url = observability.endpoint(host)
    try:
        reply = httpx.post(url, headers=observability.auth_header(),
                           content=PROBE_BODY, timeout=20.0)
    except Exception as exc:                        # noqa: BLE001
        return None, f"could not reach it: {type(exc).__name__}: {str(exc)[:120]}"
    note = MEANING.get(reply.status_code,
                       f"unexpected status {reply.status_code}")
    return reply.status_code, note


def send_one_span(host: str) -> bool:
    """Send a single synthetic span and report whether the server took it."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExportResult, SimpleSpanProcessor

    class Watched(OTLPSpanExporter):
        last = None

        def export(self, spans):
            Watched.last = super().export(spans)
            return Watched.last

    exporter = Watched(endpoint=observability.endpoint(host),
                       headers=observability.auth_header())
    provider = TracerProvider(resource=Resource.create({
        "service.name": "claimsight-connectivity-check"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("claimsight.check")
    with tracer.start_as_current_span("claimsight connectivity check") as span:
        span.set_attribute("check.kind", "synthetic")
        span.set_attribute("check.sent_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        span.set_attribute("check.contains_claim_data", False)
    provider.force_flush()
    provider.shutdown()
    return Watched.last == SpanExportResult.SUCCESS


def main() -> None:
    facts = observability.diagnose()
    print("\nWhat is configured\n" + "-" * 62)
    for key in ("public_key", "secret_key", "host", "host_explicitly_set",
                "endpoint", "send_content"):
        print(f"  {key:22} {facts[key]}")

    if not observability.configured():
        print("\nNo keys are set, so there is nothing to check.")
        print("Put LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .env, then run again.")
        sys.exit(1)

    public = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret = os.getenv("LANGFUSE_SECRET_KEY", "")
    if not public.startswith("pk-"):
        print("\nnote: the public key does not start with 'pk-'. "
              "Check the two keys are not the wrong way round.")
    if not secret.startswith("sk-"):
        print("\nnote: the secret key does not start with 'sk-'. "
              "Check the two keys are not the wrong way round.")

    hosts = ([facts["host"]] if facts["host_explicitly_set"]
             else list(observability.KNOWN_HOSTS))
    print("\nCan these credentials authenticate?\n" + "-" * 62)
    accepted = None
    for candidate in hosts:
        status, note = probe(candidate)
        print(f"  {candidate:34} {status or '-'}  {note}")
        if status in (200, 202, 400) and accepted is None:
            accepted = candidate

    if accepted is None:
        print("\nRESULT: remote export NOT verified. The server did not accept these "
              "credentials at any address tried.")
        if not facts["host_explicitly_set"]:
            print("If your Langfuse project is in the US region, set "
                  "LANGFUSE_HOST=https://us.cloud.langfuse.com in .env.")
        sys.exit(1)

    if accepted != facts["host"]:
        print(f"\nThe credentials work against {accepted}, not the configured "
              f"{facts['host']}.")
        print(f"Set LANGFUSE_HOST={accepted} in .env.")

    print("\nSending one synthetic span\n" + "-" * 62)
    if send_one_span(accepted):
        print("  the server accepted the span")
        print(f"\nRESULT: remote export VERIFIED against {accepted}.")
        print("Look for a trace named 'claimsight connectivity check' in Langfuse.")
        print("It carries no claim data: only a name, a timestamp and a marker.")
        sys.exit(0)

    print("  the server did not accept the span")
    print("\nRESULT: authentication worked but the trace was not accepted. "
          "Nothing was exported.")
    sys.exit(1)


if __name__ == "__main__":
    main()
