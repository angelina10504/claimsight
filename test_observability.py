"""
Test the tracing: one trace per claim within a process, carrying no PII.

Costs no tokens and needs no Langfuse account. Instead of sending spans to
Langfuse it collects them in memory, which is the same path with a different
exporter at the end, so what is tested is what would be sent.

    tracing stays off until the Langfuse keys are set
    a CrewAI run produces spans for the crew and the agent
    an ADK run in the same process lands in the same provider and trace (the
      live demo runs ADK and CrewAI in two processes, which this does not cover)
    the claim span wraps them and carries the claim id
    a phone number in a span never leaves the machine
    with LANGFUSE_TRACE_CONTENT=false, no text leaves at all
    the trace id is recorded in the claim's own log, so the two can be read
      side by side

Run:  uv run python test_observability.py
"""

import os
# No offline test sends anything to Langfuse: without this, a test that
# starts the A2A server would pick up the real keys from .env and post
# spans, which is how an automated run came to report a 401.
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

# Isolate this test from .env. config.py, observability.py and the ADK agent
# each call load_dotenv(), which fills in any variable that is *missing* but
# never overrides one that is present. Popping a key is therefore undone by
# the next import; setting it to "" holds. The provider keys are blanked too,
# so the ADK section cannot make a real Gemini call.
ISOLATED = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
            "LANGFUSE_BASE_URL", "LANGFUSE_TRACE_CONTENT",
            "GOOGLE_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY")
for _key in ISOLATED:
    os.environ[_key] = ""

import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

PHONE = "9876543210"


def scripted_llm(reply: str):
    from crewai.llms.base_llm import BaseLLM

    class Scripted(BaseLLM):
        def supports_function_calling(self):
            return False

        def call(self, messages, tools=None, callbacks=None, available_functions=None, **kw):
            return reply

    return Scripted(model="scripted")


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" +
              (f"\n        {detail}" if detail else ""))

    # ---------------------------------------------------------------- off by default
    print("1. Without Langfuse keys, nothing is traced\n")
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        os.environ[key] = ""                          # "", not pop: see ISOLATED
    os.environ["CLAIMSIGHT_OFFLINE"] = ""             # section 1 needs it genuinely off
    import observability
    run("tracing is not configured", not observability.configured())
    run("start() declines rather than failing", observability.start() is False)
    run("it says so plainly", "tracing off" in observability.describe(),
        observability.describe())
    run("a claim span is a no-op, so callers need no condition",
        observability.claim_span("CLM-0000") is not None)
    run("no trace id to record", observability.trace_id() is None)

    print("\n2. Configuration, diagnosed without showing a secret\n")
    os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-1234567890abcdef1234"
    os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-abcdef1234567890abcd"
    facts = observability.diagnose()
    blob = json.dumps(facts)
    run("it reports that the keys are set", "set, starts pk-lf-" in facts["public_key"],
        facts["public_key"])
    run("without putting the secret in the output",
        os.environ["LANGFUSE_SECRET_KEY"] not in blob
        and os.environ["LANGFUSE_PUBLIC_KEY"] not in blob)
    run("it says which endpoint would be used",
        facts["endpoint"].endswith("/api/public/otel/v1/traces"), facts["endpoint"])
    run("an unset host falls back to the default region, and says it is a fallback",
        facts["host"] == observability.DEFAULT_HOST
        and facts["host_explicitly_set"] is False, facts["host"])
    from unittest import mock
    with mock.patch.dict(os.environ):
        os.environ["LANGFUSE_BASE_URL"] = "https://us.cloud.langfuse.com/"
        from_base_url = (observability.host(), observability.host_setting())
        os.environ["LANGFUSE_HOST"] = "https://cloud.langfuse.com"
        both_set = (observability.host(), observability.host_setting())
    run("LANGFUSE_BASE_URL is used when LANGFUSE_HOST is not set",
        from_base_url == ("https://us.cloud.langfuse.com", "LANGFUSE_BASE_URL"),
        str(from_base_url))
    run("LANGFUSE_HOST wins when both are set",
        both_set == ("https://cloud.langfuse.com", "LANGFUSE_HOST"), str(both_set))
    run("and with neither, the host is back to the default",
        observability.host() == observability.DEFAULT_HOST
        and observability.host_setting() is None)
    run("both regional hosts are known, since a key works in only one",
        len(observability.KNOWN_HOSTS) == 2, list(observability.KNOWN_HOSTS))
    # The default is checked with the variable genuinely unset, and only for
    # the length of the check, so no import in between can refill it.
    from unittest import mock
    with mock.patch.dict(os.environ):
        os.environ.pop("LANGFUSE_TRACE_CONTENT", None)
        default = observability.send_content()
        os.environ["LANGFUSE_TRACE_CONTENT"] = "true"
        asked_for = observability.send_content()
    run("by default, with the variable unset, content is withheld", default is False)
    run("content is sent only when asked for", asked_for is True)
    run("diagnose() reports the effective value, not the default",
        facts["send_content"] is observability.send_content())

    print("\n   offline mode\n")
    os.environ["CLAIMSIGHT_OFFLINE"] = "1"
    run("offline mode is recognised", observability.offline() is True)
    run("and the real endpoint is still only reported, never used",
        "cloud.langfuse.com" in observability.endpoint())

    # ---------------------------------------------------------------- with an exporter
    print("\n3. With tracing on: a CrewAI run\n")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    memory = InMemorySpanExporter()
    run("tracing starts", observability.start(exporter=memory) is True)

    from crewai import Agent, Crew, Task
    agent = Agent(role="Probe Analyst", goal="g", backstory="b",
                  llm=scripted_llm(f"Thought: done.\nFinal Answer: reach the claimant on {PHONE}"))
    task = Task(description="Report on claim CLM-5000", expected_output="a line", agent=agent)

    with observability.claim_span("CLM-5000", route="auto_approve") as span:
        traced = observability.trace_id()
        Crew(agents=[agent], tasks=[task], verbose=False).kickoff()
    observability.flush()

    spans = memory.get_finished_spans()
    names = [s.name for s in spans]
    run("spans were produced", len(spans) >= 3, f"{len(spans)}: {names}")
    run("the crew appears", any("kickoff" in n for n in names))
    run("the agent appears", any("Probe Analyst" in n for n in names))
    run("the claim span wraps them", "claim CLM-5000" in names)

    claim_spans = [s for s in spans if s.name == "claim CLM-5000"]
    attributes = dict(claim_spans[0].attributes or {}) if claim_spans else {}
    run("the claim span carries the claim id", attributes.get("claim.id") == "CLM-5000",
        json.dumps({k: str(v) for k, v in attributes.items()}))
    run("and the route it took", attributes.get("claim.route") == "auto_approve")
    run("the trace id is a 32-character hex string",
        bool(traced) and len(traced) == 32 and all(c in "0123456789abcdef" for c in traced),
        traced)
    run("every span shares that trace",
        all(format(s.context.trace_id, "032x") == traced for s in spans))

    print("\n4. What a span is allowed to carry out\n")
    withheld_by_default = observability.scrub_attributes(
        {"input.value": "reach the claimant on " + PHONE})
    run("by default, text does not leave at all",
        PHONE not in json.dumps(withheld_by_default),
        json.dumps(withheld_by_default))
    exported = json.dumps([{k: str(v) for k, v in (s.attributes or {}).items()}
                           for s in spans])
    run("a phone number in a prompt or an answer does not leave the machine",
        PHONE not in exported)

    shared = observability.scrub_attributes(
        {"input.value": "reach the claimant on " + PHONE,
         "llm.input_messages": ["my number is " + PHONE, "noted"],
         "llm.token_count.total": 1234}, send_content=True)
    run("with content allowed, a phone number is still masked",
        PHONE not in json.dumps(shared), json.dumps(shared)[:120])
    run("the rest of the text is kept, so the trace is still useful",
        "claimant" in json.dumps(shared).lower())
    run("a list of messages is cleaned too, not just a single string",
        PHONE not in json.dumps(shared["llm.input_messages"]),
        json.dumps(shared["llm.input_messages"]))

    withheld = observability.scrub_attributes(
        {"input.value": "reach the claimant on " + PHONE,
         "llm.token_count.total": 1234,
         "llm.input_messages": ["my number is " + PHONE],
         "output.value": "approved"}, send_content=False)
    run("with content off, text is withheld",
        "claimant" not in json.dumps(withheld), json.dumps(withheld))
    run("and so are lists of messages",
        PHONE not in json.dumps(withheld["llm.input_messages"]),
        json.dumps(withheld["llm.input_messages"]))
    run("but token counts and numbers still go, so cost stays measurable",
        withheld.get("llm.token_count.total") == 1234)
    kept = observability.scrub_attributes({"llm.model_name": "gpt-oss-120b"}, send_content=False)
    run("names of models and tools are not text to withhold",
        kept.get("llm.model_name") == "gpt-oss-120b")

    # ---------------------------------------------------------------- both frameworks
    print("\n5. ADK in the same process reports into the same provider and trace\n")
    before = len(memory.get_finished_spans())
    import asyncio

    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    import logging
    logging.disable(logging.CRITICAL)      # ADK logs the missing-key error loudly

    async def drive():
        adk_agent = LlmAgent(name="probe_assistant", model="gemini-2.5-flash",
                             instruction="say hello")
        sessions = InMemorySessionService()
        await sessions.create_session(app_name="p", user_id="u", session_id="s")
        runner = Runner(agent=adk_agent, app_name="p", session_service=sessions)
        try:
            async for _ in runner.run_async(
                    user_id="u", session_id="s",
                    new_message=types.Content(role="user", parts=[types.Part(text="hi")])):
                pass
        except Exception:        # no Gemini key here; the spans are the point
            pass

    with observability.claim_span("CLM-5000", stage="claimant assistant"):
        asyncio.run(drive())
    observability.flush()

    logging.disable(logging.NOTSET)
    print("   (ADK's model call fails here: there is no Gemini key in a test, "
          "and the spans are the point)\n")

    adk_names = [s.name for s in memory.get_finished_spans()[before:]]
    run("ADK produced spans without any extra wiring", bool(adk_names),
        f"{len(adk_names)}: {adk_names[:5]}")
    run("they include the agent ADK invoked",
        any("probe_assistant" in n for n in adk_names))
    run("and the model call, which is where tokens and latency come from",
        any("generate_content" in n for n in adk_names), adk_names[:5])

    # ---------------------------------------------------------------- the local log
    print("\n6. The claim's own log points at its trace\n")
    import tempfile

    import monitoring
    monitoring.LOGS = Path(tempfile.mkdtemp(prefix="claimsight_trace_"))
    with observability.claim_span("CLM-5000"):
        expected = observability.trace_id()
        monitor = monitoring.ClaimMonitor("CLM-5000")
        monitor.finish(status="approved")
    written = [json.loads(line) for line in
               (monitoring.LOGS / "CLM-5000.jsonl").read_text().splitlines() if line.strip()]
    started = next((e for e in written if e["event"] == "run_started"), {})
    finished = next((e for e in written if e["event"] == "run_finished"), {})
    run("the log records the trace the run belongs to",
        started.get("trace_id") == expected, f"{started.get('trace_id')}")
    run("and records it again at the end, where the summary is",
        finished.get("trace_id") == expected)

    print("\n7. Span events carry content too\n")
    from opentelemetry.sdk.trace import Event
    events = (Event(name="exception",
                    attributes={"exception.message": "failed for " + PHONE},
                    timestamp=1),)
    kept = observability._scrub_events(events, send_content=True)
    run("an exception message in an event is cleaned",
        PHONE not in json.dumps({k: str(v) for k, v in
                                 (kept[0].attributes or {}).items()}),
        json.dumps({k: str(v) for k, v in (kept[0].attributes or {}).items()}))
    run("with content off, events are dropped entirely",
        observability._scrub_events(events, send_content=False) == ())

    print("\n8. Nothing in the offline tests reaches the network\n")
    offline_tests = sorted(Path(ROOT).glob("test_*.py"))
    guarded = [p for p in offline_tests if "CLAIMSIGHT_OFFLINE" in p.read_text()]
    run("every offline test declares itself offline",
        len(guarded) >= 11, f"{len(guarded)} of {len(offline_tests)} test files")
    run("this one is among them", "CLAIMSIGHT_OFFLINE" in Path(__file__).read_text())

    print("\n9. A second start() with an exporter replaces the first\n")
    second = InMemorySpanExporter()
    first_count = len(memory.get_finished_spans())
    run("start() with a new exporter still reports tracing on",
        observability.start(exporter=second) is True)
    with observability.claim_span("CLM-0001"):
        pass
    observability.flush()
    run("spans now go to the new exporter",
        [s.name for s in second.get_finished_spans()] == ["claim CLM-0001"],
        str([s.name for s in second.get_finished_spans()]))
    run("and no longer to the old one",
        len(memory.get_finished_spans()) == first_count)
    run("collected() reads the new exporter",
        [s.name for s in observability.collected()] == ["claim CLM-0001"])

    print(f"\n{passed}/{total} tracing checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
