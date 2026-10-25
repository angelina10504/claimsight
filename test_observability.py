"""
Test the tracing: one trace per claim, across both frameworks, carrying no PII.

Costs no tokens and needs no Langfuse account. Instead of sending spans to
Langfuse it collects them in memory, which is the same path with a different
exporter at the end, so what is tested is what would be sent.

    tracing stays off until the Langfuse keys are set
    a CrewAI run produces spans for the crew and the agent
    an ADK run produces spans in the same provider, so one claim is one trace
    the claim span wraps them and carries the claim id
    a phone number in a span never leaves the machine
    with LANGFUSE_TRACE_CONTENT=false, no text leaves at all
    the trace id is recorded in the claim's own log, so the two can be read
      side by side

Run:  uv run python test_observability.py
"""

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
        os.environ.pop(key, None)
    import observability
    run("tracing is not configured", not observability.configured())
    run("start() declines rather than failing", observability.start() is False)
    run("it says so plainly", "tracing off" in observability.describe(),
        observability.describe())
    run("a claim span is a no-op, so callers need no condition",
        observability.claim_span("CLM-0000") is not None)
    run("no trace id to record", observability.trace_id() is None)

    # ---------------------------------------------------------------- with an exporter
    print("\n2. With tracing on: a CrewAI run\n")
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

    print("\n3. What a span is allowed to carry out\n")
    exported = json.dumps([{k: str(v) for k, v in (s.attributes or {}).items()}
                           for s in spans])
    run("a phone number in a prompt or an answer does not leave the machine",
        PHONE not in exported)
    run("it is replaced by a marker, so the trace still reads sensibly",
        "[phone redacted]" in exported)
    run("the rest of the text is kept, so the trace is still useful",
        "claimant" in exported.lower())

    withheld = observability.scrub_attributes(
        {"input.value": "reach the claimant on " + PHONE,
         "llm.token_count.total": 1234,
         "output.value": "approved"}, send_content=False)
    run("with content off, text is withheld",
        "claimant" not in json.dumps(withheld), json.dumps(withheld))
    run("but token counts and numbers still go, so cost stays measurable",
        withheld.get("llm.token_count.total") == 1234)
    kept = observability.scrub_attributes({"llm.model_name": "gpt-oss-120b"}, send_content=False)
    run("names of models and tools are not text to withhold",
        kept.get("llm.model_name") == "gpt-oss-120b")

    # ---------------------------------------------------------------- both frameworks
    print("\n4. ADK reports into the same provider, so one claim is one trace\n")
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
    print("\n5. The claim's own log points at its trace\n")
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

    print(f"\n{passed}/{total} tracing checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
