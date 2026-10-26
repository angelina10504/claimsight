"""
Test what the Flow does with the photograph: the Damage Analyst's findings,
compared with the invoice in code, decide the route.

Costs no tokens. The crews are replaced by stand-ins - the intake stand-in
returns fixed photo findings, the assessment stand-in a clean, low-risk
decision - and every folder the Flow writes to is redirected to a temporary
one. The claims are copies of photo claim CLM-6001 under demo ids, so no real
claim, decision or log is touched.

    a billed part the photo shows UNDAMAGED -> fraud flag, human review,
                                               reason names the part; code
                                               decides, whatever the Lead scored
    a billed part the photo does NOT SHOW   -> evidence request for a photo
                                               of that part, no fraud flag
        ... the photo arrives               -> a person inspects it; no loop
    an unreadable photo                     -> a clearer photo is requested
    every billed part seen damaged          -> nothing changes: auto-approve
    no photo findings at all                -> nothing changes

Run:  CLAIMSIGHT_OFFLINE=1 uv run python test_photo_flow.py
"""

import os

os.environ["CLAIMSIGHT_OFFLINE"] = "1"
for _key in ("GROQ_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
             "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ[_key] = ""                # "" survives load_dotenv; a pop does not

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

import claim_flow      # noqa: E402
import evidence        # noqa: E402
import monitoring      # noqa: E402
import reporting       # noqa: E402

SOURCE = "CLM-6001"    # billed: fender, front door, painting, labour
PARTS_12 = ["front bumper", "rear bumper", "headlight", "taillight", "side mirror",
            "grille", "bonnet", "front door", "rear door", "fender", "windshield",
            "door glass"]
FINDINGS = {}          # claim id -> the photo_damage the stand-in intake returns
calls = {"intake": 0, "assessment": 0}


def seen(damaged=(), undamaged=(), readable=True):
    rest = [p for p in PARTS_12 if p not in damaged and p not in undamaged]
    return [{"photo": "05bf64c441a8b9f6e0b9.jpg", "damaged": list(damaged),
             "undamaged": list(undamaged), "not_visible": rest, "damage_types": ["dent"],
             "severity": "moderate", "readable": readable, "notes": ""}]


def stand_in_intake(claim_id: str, verbose: bool) -> dict:
    calls["intake"] += 1
    claim = json.loads((claim_flow.CLAIMS / f"{claim_id}.json").read_text())
    policy = json.loads((claim_flow.POLICIES / f"{claim['policy_no']}.json").read_text())
    lines = [{k: item[k] for k in ("description", "amount", "material")}
             for item in claim["line_items"]]
    return {"claim_id": claim_id, "policy_no": claim["policy_no"],
            "incident_date": claim["incident_date"], "narrative": claim["narrative"],
            "registration": policy["registration"],
            "damaged_parts": ["fender", "front door"],       # from the invoice, as the crew does
            "invoice": {"invoice_no": "INV-TEST", "registration": policy["registration"],
                        "total": claim["invoice_total"], "line_items": lines},
            "photo_damage": FINDINGS.get(claim_id, []),
            "is_complete": True, "missing_fields": []}


def stand_in_assessment(inputs: dict, verbose: bool) -> dict:
    """A clean, low-risk decision: if the claim goes to a person, the photo
    check sent it there, not the Lead."""
    calls["assessment"] += 1
    claim = json.loads((claim_flow.CLAIMS / f"{inputs['claim_id']}.json").read_text())
    return {"claim_id": claim["claim_id"], "coverage_status": "covered",
            "coverage_reasons": ["Section I item 6: accidental external means"],
            "uncertainty_reasons": [], "fraud_score": 0.1, "fraud_flags": [],
            "line_decisions": [{"description": i["description"], "amount": i["amount"],
                                "material": i["material"], "admitted": True, "reason": ""}
                               for i in claim["line_items"]],
            "recommended_route": "auto_approve", "summary": "stand-in assessment"}


def make_claim(claim_id: str, findings) -> None:
    claim = json.loads((ROOT / "data" / "claims" / f"{SOURCE}.json").read_text())
    claim["claim_id"], claim["split"] = claim_id, "demo"
    claim_flow.CLAIMS.mkdir(parents=True, exist_ok=True)
    (claim_flow.CLAIMS / f"{claim_id}.json").write_text(json.dumps(claim))
    FINDINGS[claim_id] = findings


def assess(claim_id: str) -> dict:
    flow = claim_flow.ClaimFlow()
    flow.kickoff(inputs={"claim_id": claim_id, "interactive": False,
                         "use_saved_intake": False})
    return flow.state.model_dump()


def logged(claim_id: str, kind: str) -> list:
    return [e for e in reporting.load_log(claim_id) if e.get("kind") == kind]


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="claimsight_photo_"))
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

    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" +
              (f"\n        {detail}" if detail else ""))

    # ---------------------------------------------------- 1. shown undamaged
    print("1. The photograph shows a billed part intact (the front door)\n")
    make_claim("CLM-9201", seen(damaged=["fender"], undamaged=["front door"]))
    state = assess("CLM-9201")
    run("the claim goes to a person", state["route"] == "human_review"
        and state["status"] == "awaiting_review", f"route={state['route']}")
    report = reporting.build_report(state)
    run("the reason names the part", "front door" in report.route_reason
        and "photo check" in report.route_reason, report.route_reason)
    run("it is a fraud flag, raised in code",
        any("Front Door" in f and "photo check, in code" in f
            for f in state["decision"]["fraud_flags"]), str(state["decision"]["fraud_flags"]))
    run("the Lead's score is left as it was: code routes, not the score",
        state["decision"]["fraud_score"] == 0.1)
    run("the check is saved in the claim state",
        state["photo_check"].get("unsupported_parts") == ["front door"])
    run("and written to the log", bool(logged("CLM-9201", "photo_check"))
        and logged("CLM-9201", "photo_check")[0].get("flagged") == ["front door"])
    run("the report shows it, line by line",
        any(l.startswith("FLAGGED") and "Front Door" in l for l in report.photo_check)
        and "PHOTOGRAPH AGAINST THE INVOICE" in reporting.render(report)
        and "## Photograph against the invoice" in reporting.to_markdown(report))
    run("and the explanation says a person will check",
        "a billed part appears undamaged" in reporting.explanation(report),
        reporting.explanation(report))
    run("nothing is authorised: the payout is only proposed",
        report.authorised_payout is None and report.proposed_payout > 0)

    # ---------------------------------------------------- 2. not visible
    print("\n2. The photograph does not show a billed part (the front door)\n")
    make_claim("CLM-9202", seen(damaged=["fender"]))
    before = dict(calls)
    state = assess("CLM-9202")
    run("the claim waits for evidence", state["status"] == "awaiting_evidence"
        and state["route"] == "needs_evidence", f"status={state['status']}")
    asked = evidence.outstanding(state)
    run("it asks the claimant for a photograph of that part",
        [a.item for a in asked] == ["photograph showing the front door"]
        and asked[0].from_whom == "claimant", str([a.item for a in asked]))
    run("and says why", "does not show this billed part" in asked[0].why, asked[0].why)
    run("it is NOT a fraud flag",
        not state["photo_check"].get("flags") and not state["decision"].get("fraud_flags")
        and not state["photo_doubts"])
    run("no assessment runs on a file still missing evidence",
        calls["assessment"] == before["assessment"])
    report = reporting.build_report(state)
    run("the report lists the request, and says it is not a flag",
        any("requested from the claimant: photograph showing the front door (not a fraud flag)"
            == l for l in report.photo_check), str(report.photo_check))

    photo = tmp / "front_door.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 2048)
    sent = evidence.submit("CLM-9202", "photograph showing the front door",
                           file=str(photo), state=state)
    run("the claimant's photograph is accepted as answering the request",
        sent["accepted"] and sent["answers_request"], sent["verdict"])
    outcome = claim_flow.resume_claim("CLM-9202", interactive=False)
    resumed = outcome["state"] or {}
    run("the claim resumes, and is not asked for the same photograph again",
        outcome["resumed"] and resumed.get("status") != "awaiting_evidence",
        outcome["why"])
    run("a person inspects the photograph: nothing examined it, so nothing is auto-approved",
        resumed.get("route") == "human_review"
        and "a person must inspect it" in reporting.build_report(resumed).route_reason,
        reporting.build_report(resumed).route_reason if resumed else "")
    run("still not a fraud flag", not resumed.get("decision", {}).get("fraud_flags"))

    # ---------------------------------------------------- 3. unreadable
    print("\n3. The photograph could not be read\n")
    make_claim("CLM-9203", seen(readable=False))
    state = assess("CLM-9203")
    run("a clearer photograph is requested, and nothing is flagged",
        state["status"] == "awaiting_evidence"
        and [a.item for a in evidence.outstanding(state)] == [claim_flow.CLEARER_PHOTO]
        and not state["photo_check"].get("flags"), str(state["photo_check"].get("requests")))

    # ---------------------------------------------------- 4. nothing to act on
    print("\n4. When the photograph agrees, or there is none\n")
    make_claim("CLM-9204", seen(damaged=["fender", "front door"]))
    state = assess("CLM-9204")
    run("every billed part seen damaged: approved as before",
        state["route"] == "auto_approve" and not state["photo_doubts"],
        f"route={state['route']}")
    make_claim("CLM-9205", [])
    state = assess("CLM-9205")
    run("no photo findings: no check, and the route is unchanged",
        state["photo_check"] == {} and state["route"] == "auto_approve"
        and not logged("CLM-9205", "photo_check"))
    run("and the report has no photo section",
        reporting.build_report(state).photo_check == []
        and "PHOTOGRAPH" not in reporting.render(reporting.build_report(state)))

    # ---------------------------------------------------- 5. the rule itself
    print("\n5. The routing rule, on its own\n")
    clean = {"coverage_status": "covered", "fraud_score": 0.0}
    route, why = claim_flow.decide_route(clean, {"payout": 9000}, [], [], [],
                                         ["the photograph shows the bonnet undamaged"])
    run("a photo problem sends a low-risk claim to a person, naming the part",
        route == "human_review" and "bonnet" in why, why)
    excluded = {"coverage_status": "excluded", "coverage_reasons": ["outside the period"]}
    route, _ = claim_flow.decide_route(excluded, {"payout": 0}, [], [], [], ["x"])
    run("an excluded claim is still rejected", route == "reject")
    run("without a photo problem the old rules are unchanged",
        claim_flow.decide_route(clean, {"payout": 9000}, [], [])[0] == "auto_approve")

    print(f"\n{passed}/{total} photo-flow checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
