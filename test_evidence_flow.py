"""
Test the full evidence cycle: ask, send, check, carry on.

Costs no tokens. The two crews are replaced by stand-ins, and every folder the
Flow writes to is redirected to a temporary one, so your real claims, intakes,
decisions and logs are untouched.

What is being tested is the behaviour that makes the system agentic rather than
merely automated: when something is missing it says exactly what, it checks
what arrives instead of trusting it, it re-runs only the part the new evidence
affects, and it does not quietly decide a claim it cannot decide.

    the claim stops and names the missing item
    a bad date is refused, with the reason
    a good date is accepted and placed in the file
    resuming re-runs the assessment but NOT the document reading
    resuming twice with nothing new changes nothing
    a file is checked for type, size and existence
    evidence nobody asked for is kept, but marked as unrequested
    "assess my claim" after sending evidence means carry on

Run:  uv run python test_evidence_flow.py
      uv run python test_evidence_flow.py --make-demo-claim   build CLM-9002 for a live demo
"""

import os
# No offline test sends anything to Langfuse: without this, a test that
# starts the A2A server would pick up the real keys from .env and post
# spans, which is how an automated run came to report a 401.
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

REAL_CLAIMS = ROOT / "data" / "claims"
REAL_INVOICES = ROOT / "data" / "invoices"
REAL_INTAKE = ROOT / "data" / "intake"
SOURCE_CLAIM = "CLM-5000"          # a clean claim: covered, nothing suspicious
DEMO_CLAIM = "CLM-9002"

import claim_flow      # noqa: E402
import evidence        # noqa: E402
import monitoring      # noqa: E402
import reporting       # noqa: E402

calls = {"intake": 0, "assessment": 0}


# ------------------------------------------------------------ stand-in crews
def stand_in_intake(claim_id: str, verbose: bool) -> dict:
    calls["intake"] += 1
    claim = json.loads((claim_flow.CLAIMS / f"{claim_id}.json").read_text())
    policy = json.loads((claim_flow.POLICIES / f"{claim['policy_no']}.json").read_text())
    lines = [{k: item[k] for k in ("description", "amount", "material")}
             for item in claim["line_items"]]
    return {"claim_id": claim_id, "policy_no": claim["policy_no"],
            "incident_date": claim.get("incident_date"), "narrative": claim["narrative"],
            "registration": policy["registration"],
            "damaged_parts": claim["ground_truth"]["damaged_parts"],
            "invoice": {"invoice_no": "INV-TEST", "registration": policy["registration"],
                        "total": claim["invoice_total"], "line_items": lines},
            "is_complete": True, "missing_fields": []}


def stand_in_assessment(inputs: dict, verbose: bool) -> dict:
    calls["assessment"] += 1
    claim = json.loads((claim_flow.CLAIMS / f"{inputs['claim_id']}.json").read_text())
    return {"claim_id": claim["claim_id"], "coverage_status": "covered",
            "coverage_reasons": ["Section I item 6: accidental external means"],
            "uncertainty_reasons": [], "fraud_score": 0.1, "fraud_flags": [],
            "line_decisions": [{"description": i["description"], "amount": i["amount"],
                                "material": i["material"], "admitted": True, "reason": ""}
                               for i in claim["line_items"]],
            "recommended_route": "auto_approve", "summary": "stand-in assessment"}


# ------------------------------------------------------------ setting up
def build_claim(folder: Path, claim_id: str, *, drop_date: bool) -> dict:
    """A copy of a real claim, optionally with its incident date missing, which
    is what makes the Flow stop and ask."""
    claim = json.loads((REAL_CLAIMS / f"{SOURCE_CLAIM}.json").read_text())
    claim["claim_id"] = claim_id
    # A demonstration fixture, never part of the official 60. The manifest is
    # what selects official claims, and this marks the file too, so no
    # split-based filter can pick it up either.
    claim["split"] = "demo"
    if drop_date:
        claim["incident_date"] = None
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{claim_id}.json").write_text(json.dumps(claim, indent=2))
    return claim


DEMO_POLICY = "POL-9002"


def make_demo_claim() -> None:
    """Create CLM-9002 in the real data folders, for a live demo.

    It is CLM-5000 with the incident date missing. Its intake is pre-saved, so
    the first run stops at the completeness gate without calling a model at
    all; only the assessment after the evidence arrives costs tokens.

    It also gets a policy of its own, POL-9002. Copied onto the original's
    policy it would share that policy's parts and dates, and find_prior_claims
    would rightly call it a duplicate, which would bury the point of the demo."""
    claim = build_claim(REAL_CLAIMS, DEMO_CLAIM, drop_date=True)
    policies = ROOT / "data" / "policies"
    policy = json.loads((policies / f"{claim['policy_no']}.json").read_text())
    policy["policy_no"] = DEMO_POLICY
    (policies / f"{DEMO_POLICY}.json").write_text(json.dumps(policy, indent=2))
    claim["policy_no"] = DEMO_POLICY
    (REAL_CLAIMS / f"{DEMO_CLAIM}.json").write_text(json.dumps(claim, indent=2))
    source_invoice = REAL_INVOICES / f"{SOURCE_CLAIM}.pdf"
    if source_invoice.exists():
        shutil.copy2(source_invoice, REAL_INVOICES / f"{DEMO_CLAIM}.pdf")

    saved = REAL_INTAKE / f"{SOURCE_CLAIM}.json"
    if saved.exists():
        intake = json.loads(saved.read_text())
    else:
        intake = {"claim_id": DEMO_CLAIM, "policy_no": DEMO_POLICY,
                  "narrative": claim["narrative"], "registration": policy["registration"],
                  "damaged_parts": claim["ground_truth"]["damaged_parts"],
                  "invoice": {"invoice_no": "INV-9002",
                              "registration": policy["registration"],
                              "total": claim["invoice_total"],
                              "line_items": [{k: i[k] for k in
                                              ("description", "amount", "material")}
                                             for i in claim["line_items"]]}}
    intake["claim_id"] = DEMO_CLAIM
    intake["policy_no"] = DEMO_POLICY
    intake["incident_date"] = None
    intake["is_complete"] = False
    intake["missing_fields"] = ["incident date"]
    REAL_INTAKE.mkdir(parents=True, exist_ok=True)
    (REAL_INTAKE / f"{DEMO_CLAIM}.json").write_text(json.dumps(intake, indent=2))

    for folder in (claim_flow.DECISIONS, claim_flow.REVIEW_QUEUE, evidence.EVIDENCE):
        stale = folder / f"{DEMO_CLAIM}.json"
        if stale.exists():
            stale.unlink()

    print(f"Built {DEMO_CLAIM}: {SOURCE_CLAIM} with no incident date, "
          f"on its own policy {DEMO_POLICY}.\n")
    print("Live demo, in order:")
    print(f"  1. uv run python run_flow.py {DEMO_CLAIM} --no-input")
    print("       it stops and asks for the incident date (no model called)")
    print(f"  2. uv run python add_evidence.py {DEMO_CLAIM}")
    print("       shows what is outstanding")
    print(f'  3. uv run python add_evidence.py {DEMO_CLAIM} --item "incident date" '
          f'--value 30-05-2026')
    print("       refused: the date must be YYYY-MM-DD")
    print(f'  4. uv run python add_evidence.py {DEMO_CLAIM} --item "incident date" '
          f'--value 2026-05-30 --resume --no-input')
    print("       accepted, and the claim carries on (one assessment, ~40K tokens)")
    print(f"  5. uv run python show_report.py {DEMO_CLAIM}")
    print("       the report shows the request, the evidence and the decision")


# ------------------------------------------------------------ the test
def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="claimsight_evidence_"))
    claim_flow.CLAIMS = tmp / "claims"
    claim_flow.INTAKE = tmp / "intake"
    claim_flow.DECISIONS = tmp / "decisions"
    claim_flow.REVIEW_QUEUE = tmp / "review_queue"
    monitoring.LOGS = tmp / "logs"
    evidence.EVIDENCE = tmp / "evidence"
    reporting.DECISIONS, reporting.REVIEW_QUEUE = claim_flow.DECISIONS, claim_flow.REVIEW_QUEUE
    reporting.LOGS, reporting.REPORTS = monitoring.LOGS, tmp / "reports"
    claim_flow.run_intake_step = stand_in_intake
    claim_flow.run_assessment_step = stand_in_assessment

    claim_id = "CLM-9101"
    build_claim(claim_flow.CLAIMS, claim_id, drop_date=True)

    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" +
              (f"\n        {detail}" if detail else ""))

    # ---------------------------------------------------- 1. the claim stops and asks
    print("1. A claim with no incident date\n")
    flow = claim_flow.ClaimFlow()
    flow.kickoff(inputs={"claim_id": claim_id, "interactive": False})
    state = flow.state.model_dump()
    run("the claim waits instead of being decided",
        state["status"] == "awaiting_evidence" and state["route"] == "needs_evidence",
        f"status={state['status']}, route={state['route']}")
    run("no assessment was run on an incomplete file", calls["assessment"] == 0)
    asked = evidence.outstanding(state)
    run("it names the missing item and who must send it",
        len(asked) == 1 and asked[0].from_whom == "claimant"
        and "incident date" in asked[0].item, f"{[a.item for a in asked]}")

    # ---------------------------------------------------- 2. bad evidence is refused
    print("\n2. The claimant sends the date in the wrong form\n")
    bad = evidence.submit(claim_id, "incident date", value="30-05-2026", state=state)
    run("it is refused, with the reason", not bad["accepted"], bad["verdict"])
    run("nothing was applied to the claim", not evidence.unapplied(claim_id))
    outcome = claim_flow.resume_claim(claim_id, interactive=False)
    run("the claim cannot be resumed on refused evidence", not outcome["resumed"],
        outcome["why"])
    future = evidence.submit(claim_id, "incident date", value="2099-01-01", state=state)
    run("a date in the future is refused too", not future["accepted"], future["verdict"])

    # ---------------------------------------------------- 3. good evidence is accepted
    print("\n3. The claimant sends a proper date\n")
    good = evidence.submit(claim_id, "incident date", value="2026-05-30", state=state)
    run("it is accepted", good["accepted"], good["verdict"])
    run("it is recognised as answering what was asked", good["answers_request"] is True)
    run("it knows which field it fills", good.get("fills_field") == "incident_date")

    # ---------------------------------------------------- 4. only what is needed re-runs
    print("\n4. The claim carries on\n")
    before = dict(calls)
    outcome = claim_flow.resume_claim(claim_id, interactive=False)
    run("the claim resumed", outcome["resumed"], outcome["why"])
    run("the documents were NOT read again",
        calls["intake"] == before["intake"],
        f"intake crew ran {calls['intake'] - before['intake']} more time(s)")
    run("the assessment ran once", calls["assessment"] == before["assessment"] + 1)
    resumed = outcome["state"]
    run("the claim now has a decision", resumed["status"] == "approved",
        f"status={resumed['status']}, payout Rs {resumed['final_payout']:,.2f}")
    run("the payout was computed by the calculator, not carried over as zero",
        resumed["final_payout"] > 0)
    run("the state records what the evidence changed", bool(resumed["evidence_applied"]),
        "; ".join(resumed["evidence_applied"]))
    run("the incident date is now in the file",
        (resumed["intake"] or {}).get("incident_date") == "2026-05-30")

    # ---------------------------------------------------- 5. nothing new, nothing done
    print("\n5. Resuming again with nothing new\n")
    again = claim_flow.resume_claim(claim_id, interactive=False)
    run("it does not re-run the assessment for nothing", not again["resumed"], again["why"])
    run("the assessment really did not run again",
        calls["assessment"] == before["assessment"] + 1)

    # ---------------------------------------------------- 6. the report tells the story
    print("\n6. What the report says afterwards\n")
    report = reporting.for_claim(claim_id)
    run("nothing is outstanding any more", not report.outstanding_evidence)
    submitted = [e for e in report.evidence if "submitted evidence" in e.source]
    run("every submission is on the record, refused ones included",
        len(submitted) == 3, f"{len(submitted)} entries")
    run("the refused date is shown as refused",
        any("REJECTED" in e.detail and "30-05-2026" in e.detail for e in submitted))
    run("the accepted date is shown as applied",
        any(e.grounded and "applied" in e.detail for e in submitted))

    # ---------------------------------------------------- 7. files are checked
    print("\n7. A file as evidence\n")
    photo = tmp / "windshield.jpg"
    photo.write_bytes(b"\xff\xd8\xff" + b"0" * 2048)
    empty = tmp / "empty.jpg"
    empty.write_bytes(b"")
    wrong = tmp / "notes.docx"
    wrong.write_bytes(b"0" * 100)

    missing_file = evidence.submit(claim_id, "windshield photograph",
                                   file=str(tmp / "nope.jpg"))
    run("a file that is not there is refused", not missing_file["accepted"],
        missing_file["verdict"])
    run("an empty file is refused",
        not evidence.submit(claim_id, "windshield photograph", file=str(empty))["accepted"])
    bad_type = evidence.submit(claim_id, "windshield photograph", file=str(wrong))
    run("an unsupported file type is refused", not bad_type["accepted"], bad_type["verdict"])
    kept = evidence.submit(claim_id, "windshield photograph", file=str(photo))
    run("a real photograph is accepted and kept with the claim", kept["accepted"],
        f"{kept['verdict']} -> {kept.get('file')}")
    run("the file was copied, not just referenced",
        (ROOT / kept["file"]).exists() if kept.get("file") else False)

    # ---------------------------------------------------- 8. unrequested evidence
    print("\n8. Evidence nobody asked for\n")
    state = claim_flow.saved_state(claim_id)
    extra = evidence.submit(claim_id, "my holiday photos", value="see attached",
                            state=state)
    run("it is kept but marked as not asked for",
        extra["accepted"] and extra["answers_request"] is False, extra["verdict"])

    # ---------------------------------------------------- 9. the claimant's own words
    print('\n9. "I sent it, please assess my claim" (the A2A path)\n')
    second = "CLM-9102"
    build_claim(claim_flow.CLAIMS, second, drop_date=True)
    claim_flow.run_or_resume(second, interactive=False)
    waiting = claim_flow.saved_state(second)
    run("the first attempt waits for the date", waiting["status"] == "awaiting_evidence")
    evidence.submit(second, "incident date", value="2026-05-30", state=waiting)
    mark = dict(calls)
    final = claim_flow.run_or_resume(second, interactive=False)
    run("asking again carries on rather than starting over",
        final["status"] == "approved" and final["resumed"] is True,
        f"status={final['status']}, resumed={final['resumed']}")
    run("and it still did not read the documents again",
        calls["intake"] == mark["intake"])

    # ---------------------------------------------------- 10. a failed reassessment
    print("\n10. The reassessment fails part-way\n")
    third = "CLM-9103"
    build_claim(claim_flow.CLAIMS, third, drop_date=True)
    claim_flow.run_or_resume(third, interactive=False)
    waiting_state = claim_flow.saved_state(third)
    sent = evidence.submit(third, "incident date", value="2026-05-30",
                           state=waiting_state)
    intake_path = claim_flow.INTAKE / f"{third}.json"
    intake_before = intake_path.read_text() if intake_path.exists() else None
    decision_before = (claim_flow.DECISIONS / f"{third}.json").read_text()

    def explode(inputs, verbose):
        calls["assessment"] += 1
        raise RuntimeError("Error code: 429 - rate limit reached")

    good_assessment = claim_flow.run_assessment_step
    claim_flow.run_assessment_step = explode
    outcome = claim_flow.resume_claim(third, interactive=False)
    claim_flow.run_assessment_step = good_assessment

    run("the resume reports failure rather than a decision", not outcome["resumed"],
        outcome["why"][:110])
    ledger_items = [i for i in evidence.ledger(third)["items"] if i.get("accepted")]
    run("the evidence is NOT marked applied",
        all(evidence.state_of(i) != evidence.APPLIED for i in ledger_items),
        [evidence.state_of(i) for i in ledger_items])
    run("it is back in the queue, so the claimant need not send it again",
        len(evidence.unapplied(third)) == 1,
        [evidence.state_of(i) for i in ledger_items])
    run("the failure is recorded against it",
        "429" in (ledger_items[0].get("last_error") or ""),
        ledger_items[0].get("last_error"))
    run("the attempt was counted", ledger_items[0].get("attempts") == 1,
        f"attempts={ledger_items[0].get('attempts')}")
    run("the saved intake was rolled back, not left half-patched",
        (intake_path.read_text() if intake_path.exists() else None) == intake_before)
    run("no stale decision was written",
        (claim_flow.DECISIONS / f"{third}.json").read_text() == decision_before)
    run("the claim still reads as waiting for evidence",
        claim_flow.saved_state(third)["status"] == "awaiting_evidence")

    # ---------------------------------------------------- 11. retry succeeds
    print("\n11. Trying again, with the model working\n")
    retry = claim_flow.resume_claim(third, interactive=False)
    run("the retry goes through", retry["resumed"], retry["why"][:90])
    run("the claim now has a decision", retry["state"]["status"] == "approved",
        f"{retry['state']['status']}, Rs {retry['state']['final_payout']:,.2f}")
    after = [i for i in evidence.ledger(third)["items"] if i.get("accepted")]
    run("only now is the evidence marked applied",
        evidence.state_of(after[0]) == evidence.APPLIED)
    run("it took two attempts, and says so", after[0].get("attempts") == 2,
        f"attempts={after[0].get('attempts')}")
    run("a further resume does nothing",
        not claim_flow.resume_claim(third, interactive=False)["resumed"])

    # ---------------------------------------------------- 12. duplicates
    print("\n12. The same evidence sent twice\n")
    state = claim_flow.saved_state(third)
    again = evidence.submit(third, "incident date", value="2026-05-30", state=state)
    run("the second submission is recognised as the same one",
        again.get("duplicate") is True, again["verdict"][:100])
    run("it did not add a second ledger entry",
        len([i for i in evidence.ledger(third)["items"]
             if i["item"] == "incident date" and i.get("accepted")]) == 1)
    run("and it did not queue another reassessment",
        not evidence.unapplied(third))

    photo_a = tmp / "same.jpg"
    photo_a.write_bytes(b"\xff\xd8\xff" + b"7" * 4096)
    photo_b = tmp / "renamed.jpg"
    photo_b.write_bytes(photo_a.read_bytes())
    first_photo = evidence.submit(third, "windshield photograph", file=str(photo_a))
    same_bytes = evidence.submit(third, "windshield photograph", file=str(photo_b))
    run("the same photograph under another name is still the same photograph",
        same_bytes.get("duplicate") is True, same_bytes["verdict"][:90])
    other = tmp / "different.jpg"
    other.write_bytes(b"\xff\xd8\xff" + b"9" * 4096)
    run("a genuinely different photograph is accepted",
        not evidence.submit(third, "windshield photograph",
                            file=str(other)).get("duplicate"))

    print("\n13. Identifiers, old ledgers and safe writes\n")
    ids = [i["id"] for i in evidence.ledger(third)["items"] if i.get("id")]
    run("every item has its own identifier", len(ids) == len(set(ids)) and len(ids) >= 3,
        f"{len(ids)} ids, all different")
    run("identifiers are stable across reads",
        ids == [i["id"] for i in evidence.ledger(third)["items"] if i.get("id")])

    old_format = {"claim_id": "CLM-9104", "items": [
        {"item": "invoice total", "kind": "value", "value": 1000, "accepted": True,
         "applied": True, "verdict": "ok", "submitted_at": "2026-10-01T10:00:00"},
        {"item": "incident date", "kind": "value", "value": "2026-05-30",
         "accepted": True, "applied": False, "verdict": "ok",
         "submitted_at": "2026-10-01T10:00:01"}]}
    evidence.write_json(evidence.path_for("CLM-9104"), old_format)
    states = [evidence.state_of(i) for i in evidence.ledger("CLM-9104")["items"]]
    run("a ledger written before states existed still reads correctly",
        states == [evidence.APPLIED, evidence.PENDING], states)
    run("and its unapplied item is still found",
        len(evidence.unapplied("CLM-9104")) == 1)

    target = tmp / "atomic.json"
    evidence.write_json(target, {"keep": "this"})
    try:
        # A tuple key cannot be JSON, and default=str does not apply to keys,
        # so this fails after the temporary file has been partly written.
        evidence.write_json(target, {("a", "b"): 1})
    except TypeError:
        pass
    run("a write that fails leaves the previous file intact",
        json.loads(target.read_text()) == {"keep": "this"},
        target.read_text()[:60])
    run("and leaves no temporary files behind",
        not [f for f in tmp.iterdir() if f.name.startswith(".atomic.json")],
        [f.name for f in tmp.iterdir() if f.name.startswith(".")])

    print(f"\n{passed}/{total} evidence-cycle checks as expected")
    print(f"(temporary files in {tmp})")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    if "--make-demo-claim" in sys.argv:
        make_demo_claim()
    else:
        main()
