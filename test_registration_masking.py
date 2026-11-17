"""
Vehicle registration masking on the way OUT of the system, without a token.

Trace spans can carry prompts and tool results (LANGFUSE_TRACE_CONTENT=true), and
the intake crew sees the policy's registration. The scrubbing exporter masks it.
The agents themselves still receive it: redact_pii's default (used on every
message sent to a model) is unchanged, because the intake check compares the
invoice's registration with the policy's.

    every registration in the dataset is masked on the way out
    standard, spaced, hyphenated, Delhi-style and BH-series plates are masked
    claim ids, policy numbers, invoice numbers, GSTINs and amounts are not
    the default redact_pii (what the agents get) keeps the registration
    the span scrubber masks it under any attribute key, nested lists, events
    with content off, nothing textual is sent at all (unchanged)

Run:  CLAIMSIGHT_OFFLINE=1 uv run python test_registration_masking.py
"""

import os
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

import guardrails                                   # noqa: E402
import observability                                # noqa: E402

MASK = "[registration redacted]"


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))

    print("1. The plate pattern\n")
    regs = [json.loads(Path(p).read_text())["registration"]
            for p in sorted(glob.glob(str(ROOT / "data" / "policies" / "*.json")))]
    missed = [r for r in regs if guardrails.redact_pii(f"Reg. No. {r}.", outbound=True)[0]
              != f"Reg. No. {MASK}."]
    run(f"all {len(regs)} registrations in data/policies are masked", not missed, str(missed[:5]))
    for plate in ("HR38UE8148", "MH 12 AB 1234", "KA-01-HH-1234", "DL 3C AB 1234",
                  "DL3CAB1234", "GJ01A1234", "TN 09 1234", "22 BH 1234 AA", "22BH1234A"):
        out, n = guardrails.redact_pii(f"vehicle {plate} damaged", outbound=True)
        run(f"'{plate}' is masked", n == 1 and out == f"vehicle {MASK} damaged", out)
    for keep in ("CLM-6001", "POL-6001", "INV/833/26", "06AB1234C1Z5", "Rs 41,869.40",
                 "IMT 21", "Section I item 6", "JC/4611", "2026-07-15", "ISBN 1234"):
        out, n = guardrails.redact_pii(f"see {keep} here", outbound=True)
        run(f"'{keep}' is left alone", n == 0 and keep in out, out)

    print("\n2. What the agents still receive\n")
    text = "Policy registration HR38UE8148 matches the invoice."
    run("the default redact_pii (every model message) keeps the registration",
        guardrails.redact_pii(text) == (text, 0))
    run("find_pii (the decision guardrail) does not flag a registration",
        guardrails.find_pii(text) == {})

    print("\n3. The span scrubber\n")
    attrs = {
        "input.value": "Assess the claim for vehicle HR38UE8148, phone 9812345678",
        "crew_tasks": json.dumps([{"description": "Policy schedule: {\"registration\": \"HR38UE8148\"}"}]),
        "llm.input_messages": ["registration HR38UE8148", "nothing personal"],
        "llm.token_count.total": 4321,
        "claim.id": "CLM-6001",
    }
    on = observability.scrub_attributes(attrs, send_content=True)
    run("a registration under a textual key is masked", "HR38UE8148" not in on["input.value"]
        and MASK in on["input.value"], on["input.value"])
    run("and the phone number beside it", "9812345678" not in on["input.value"])
    run("a registration under an unremarkable key (crew_tasks) is masked too",
        "HR38UE8148" not in on["crew_tasks"] and MASK in on["crew_tasks"], on["crew_tasks"])
    run("inside a list of messages as well",
        all("HR38UE8148" not in m for m in on["llm.input_messages"]), str(on["llm.input_messages"]))
    run("numbers and ids are left as they were",
        on["llm.token_count.total"] == 4321 and on["claim.id"] == "CLM-6001")
    run("text with nothing personal is not changed",
        on["llm.input_messages"][1] == "nothing personal")
    off = observability.scrub_attributes(attrs, send_content=False)
    run("with content off, no text is sent at all (unchanged)",
        "HR38UE8148" not in json.dumps(off) and "characters not sent" in off["input.value"])

    from opentelemetry.sdk.trace import Event
    events = observability._scrub_events(
        (Event(name="exception", attributes={"exception.message": "bad input for HR38UE8148"}),),
        send_content=True)
    run("a registration in a span event is masked",
        "HR38UE8148" not in events[0].attributes["exception.message"],
        events[0].attributes["exception.message"])

    print(f"\n{passed}/{total} registration masking checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
