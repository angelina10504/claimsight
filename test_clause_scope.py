"""
The clause applicability gate, without spending a token.

A cited clause must exist in the policy wording AND be able to apply to this
policy. The wording carries the whole India Motor Tariff, so it also holds
endorsements for commercial vehicles, motor trade and two-wheelers, and add-ons
that apply only if bought. Seen in the cloud: IMT 21 (Commercial Vehicles)
used to refuse paint work on a private car, and the Road Side Assistance
add-on's parts exclusion used against a claim that never bought it.

    the scope map holds all 51 scoped blocks, read from the wording itself
    IMT 21 cited on a private car is rejected, and the reason names the scope
    the same, when it is only quoted with ellipses (as the agent wrote it)
    Road Side Assistance cited with no purchase record is rejected
    a base Section I exclusion (wear and tear, line 47) is accepted, unchanged
    a refusal citing IMT 21 becomes a coverage doubt -> human review
    a covered claim with a line denied on a scoped clause -> human review
    CLM-5007's wear-and-tear denial is NOT escalated: still auto-approve
    the decision guardrail sends a scoped-clause denial back for a retry
    IMT 22 counts as attached; IMT 26, 27 and the other 12 do not
    replay: the six real local decisions (and CLM-6001) route as before

Run:  CLAIMSIGHT_OFFLINE=1 uv run python test_clause_scope.py
"""

import os
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

import citations                                   # noqa: E402
import clause_scope as cs                          # noqa: E402
import guardrails                                  # noqa: E402
from claim_flow import assessment_inputs, coverage_doubts, decide_route   # noqa: E402

PRIVATE_CAR = {"policy_no": "POL-6001", "zero_depreciation": False}
# Reasons as the deployed agents actually wrote them (cloud runs, 18 Nov)
IMT21_QUOTED = ('Excluded under Section I exclusion clause: "the insurer shall not be liable '
                '… for loss of or damage to … paint work"')
IMT21_NAMED = "Excluded under IMT 21, special exclusions: bumpers and paint work"
RSA = ("The Company would not be liable for: any expenses for supply or replacement of "
       "parts/consumables.")
# CLM-5007's real denial, a base Section I exclusion (wording line 47)
WEAR = ("The Company shall not be liable to make any payment in respect of … wear and tear, "
        "mechanical or electrical breakdown, consequential loss, and tyres unless the "
        "vehicle was damaged at the same time.")


class FakeOutput:
    """What CrewAI hands a task guardrail: the parsed decision and the prompt."""
    def __init__(self, decision: dict, description: str):
        self.pydantic = None
        self.raw = json.dumps(decision)
        self.description = description


def decision(status, lines, reasons, score=0.1):
    route = {"covered": "auto_approve", "excluded": "reject"}.get(status, "human_review")
    return {"claim_id": "CLM-6001", "coverage_status": status, "uncertainty_reasons": [],
            "is_covered": status == "covered", "coverage_reasons": reasons,
            "line_decisions": lines, "fraud_score": score, "fraud_flags": [],
            "recommended_route": route, "summary": "test"}


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))

    # ------------------------------------------------------------ 1. the map
    print("1. The scope map\n")
    blocks = cs.scoped_blocks()
    rules = {b["rule"] for b in blocks}
    run("all 51 scoped blocks are found in the wording", len(blocks) == 51 == len(cs.SCOPED),
        str(sorted(set(cs.SCOPED) - {b["id"] for b in blocks})))
    run("27 cannot apply, 9 are add-ons, 15 depend on attachment",
        [sum(b["rule"] == r for b in blocks) for r in (cs.CANNOT, cs.IF_PURCHASED, cs.IF_ATTACHED)]
        == [27, 9, 15])
    wording = (ROOT / "knowledge" / "policy_wording.txt").read_text().split("\n")
    heads_ok = all(wording[b["first_line"] - 1].strip().upper().startswith(
        b["id"].replace("ADD-ON ", "").upper().split(".")[0].replace("IMT ", "IMT. "))
        or wording[b["first_line"] - 1].strip() == b["id"][len("ADD-ON "):]
        for b in blocks)
    run("every block starts on its own heading line", heads_ok)
    imt21 = next(b for b in blocks if b["id"] == "IMT 21")
    run("IMT 21 is lines 755-781", (imt21["first_line"], imt21["last_line"]) == (755, 781))
    run("the policy form is one named constant, not a per-policy guess",
        cs.policy_form({}) == cs.POLICY_FORM == "Private Car Package Policy"
        and cs.POLICY_FORM_HEADING in (ROOT / "knowledge" / "policy_wording.txt").read_text())

    # ------------------------------------------------------------ 2. the gate
    print("\n2. The citation check\n")
    named = citations.check(IMT21_NAMED, verdict="excluded", policy=PRIVATE_CAR)
    run("IMT 21 named on a private car is rejected", not named["applicable"]
        and not named["supports_conclusion"], named["note"])
    run("and the reason names the scope",
        named["note"] == "IMT 21 applies only to Commercial Vehicles; this policy is a "
                         "Private Car Package Policy.", named["note"])
    quoted = citations.check(IMT21_QUOTED, verdict="excluded", policy=PRIVATE_CAR)
    run("IMT 21 only quoted with ellipses, as the agent wrote it, is rejected too",
        not quoted["applicable"] and "Commercial Vehicles" in " ".join(quoted["scope_notes"]),
        str(quoted["scope_notes"]))
    rsa = citations.check(RSA, verdict="excluded", policy=PRIVATE_CAR)
    run("Road Side Assistance cited with no purchase record is rejected",
        not rsa["applicable"] and "Road Side Assistance" in rsa["note"], rsa["note"])
    wear = citations.check(WEAR, verdict="excluded", policy=PRIVATE_CAR)
    run("the base wear-and-tear exclusion (line 47) is accepted",
        wear["applicable"] and wear["scope_notes"] == [], wear["note"])
    unchanged = citations.check(WEAR, verdict="excluded")
    run("and its verdict is the same with or without the policy",
        (unchanged["source_verified"], unchanged["supports_conclusion"], unchanged["note"])
        == (wear["source_verified"], wear["supports_conclusion"], wear["note"]))
    zd = cs.applicability("ADD-ON Zero Depreciation", {"zero_depreciation": True})
    run("an add-on the policy records as bought can apply (zero depreciation)", zd[0])
    for plain in ("priced far above market rate", "same damage already claimed",
                  "outside policy period"):
        run(f"no scoped clause is read into '{plain}'", cs.cited_blocks(plain) == [])

    # ------------------------------------------------------------ 3. routing
    print("\n3. Routing\n")
    intake = {"policy_no": "POL-6001", "incident_date": "2026-11-01"}
    refused = decision("excluded", [{"description": "Painting and finishing", "amount": 4000.0,
                                     "material": "paint", "admitted": False, "reason": IMT21_NAMED}],
                       [IMT21_NAMED])
    doubts = coverage_doubts(intake, refused)
    run("a refusal resting on IMT 21 becomes a coverage doubt", bool(doubts), str(doubts))
    run("and goes to a person", decide_route(refused, {"payout": 0.0}, [], doubts)[0] == "human_review")
    covered = decision("covered", [
        {"description": "Front Door - replacement", "amount": 9000.0, "material": "metal",
         "admitted": True, "reason": ""},
        {"description": "Painting and finishing", "amount": 4000.0, "material": "paint",
         "admitted": False, "reason": IMT21_QUOTED}], ["Section I item 6"])
    doubts = coverage_doubts(intake, covered)
    run("a covered claim with a line denied on a scoped clause gets a doubt",
        bool(doubts) and "IMT 21" in doubts[0], str(doubts))
    run("which routes it to human review, not auto-approve",
        decide_route(covered, {"payout": 9000.0}, [], doubts)[0] == "human_review")

    saved = json.loads((ROOT / "data" / "assessment" / "CLM-5007.json").read_text())
    claim = json.loads((ROOT / "data" / "claims" / "CLM-5007.json").read_text())
    d5007 = saved["decision"]
    doubts = coverage_doubts({"policy_no": claim["policy_no"], "incident_date": claim["incident_date"]},
                             d5007)
    run("CLM-5007's real wear-and-tear denial raises no doubt", doubts == [], str(doubts))
    run("and CLM-5007 still auto-approves",
        decide_route(d5007, saved.get("settlement") or {}, [], doubts)[0] == "auto_approve")

    # ------------------------------------------------------------ 4. the guardrail retry
    print("\n4. The decision guardrail\n")
    prompt = ("Combine the coverage report and the fraud report for claim CLM-6001 into the "
              "final assessment.\n\nInvoice lines:\n")
    lines = [{"description": "Front Door - replacement", "amount": 9000.0, "material": "metal",
              "admitted": True, "reason": ""},
             {"description": "Painting and finishing", "amount": 4000.0, "material": "paint",
              "admitted": False, "reason": IMT21_QUOTED}]
    ok, message = guardrails.decision_guardrail(
        FakeOutput(decision("covered", lines, ["Section I item 6: accidental external means"]),
                   prompt + json.dumps([{k: x[k] for k in ("description", "amount", "material")}
                                        for x in lines], indent=1) + "\n\n"))
    run("a denial on IMT 21 is sent back for another attempt", ok is False, str(message)[:160])
    run("with the scope in the message the agent receives",
        "IMT 21 applies only to Commercial Vehicles" in str(message)
        and "Private Car Package Policy" in str(message))
    lines[1]["reason"] = WEAR
    ok, _ = guardrails.decision_guardrail(
        FakeOutput(decision("covered", lines, ["Section I item 6: accidental external means"]),
                   prompt + json.dumps([{k: x[k] for k in ("description", "amount", "material")}
                                        for x in lines], indent=1) + "\n\n"))
    run("a denial on the base wear-and-tear exclusion passes the guardrail", ok is True)

    # ------------------------------------------------------------ 5. what the analyst sees
    print("\n5. What the Policy Analyst sees\n")
    seen = json.loads(assessment_inputs({"claim_id": "CLM-6001", "policy_no": "POL-6001",
                                         "incident_date": "2026-11-01", "narrative": "",
                                         "invoice": {"line_items": []}})["policy"])
    run("the policy schedule it receives names the policy form",
        seen.get("policy_form") == "Private Car Package Policy", str(list(seen)))
    crew_src = (ROOT / "src" / "crews" / "assessment_crew.py").read_text()
    run("its instructions say an endorsement applies only within its heading's scope",
        "applies only within the scope named in its own" in crew_src)

    # ------------------------------------------------------------ 6. approved defaults
    print("\n6. Attachment-dependent endorsements (defaults approved 18 Nov)\n")
    endorsements = cs.attachment_endorsements()
    run("15 attachment-dependent endorsements are mapped", len(endorsements) == 15,
        ", ".join(b["id"] for b in endorsements))
    run("IMT 22, the compulsory deductible, is attached and can apply",
        cs.applicability("IMT 22", PRIVATE_CAR) == (True, ""))
    for ident in ("IMT 26", "IMT 27"):
        ok, why = cs.applicability(ident, PRIVATE_CAR)
        run(f"{ident} (fire and/or theft only) is not attached and cannot apply",
            not ok and "no attachment" in why, why)
    others = [b["id"] for b in endorsements if b["id"] not in ("IMT 22", "IMT 26", "IMT 27")]
    run("the other 12 are not attached", len(others) == 12
        and not any(cs.applicability(i, PRIVATE_CAR)[0] for i in others), ", ".join(others))
    fire_only = citations.check("Cover is restricted under IMT 26 to fire and theft risks only",
                                verdict="excluded", policy=PRIVATE_CAR)
    run("an accident refused under IMT 26 fails the citation check",
        not fire_only["supports_conclusion"] and "IMT 26" in fire_only["note"], fire_only["note"])
    principle = (ROOT / "src" / "clause_scope.py").read_text()
    run("the governing principle is recorded in clause_scope.py",
        "Over-blocking kills a claim; under-blocking sends it to a human" in principle)

    # ------------------------------------------------------------ 7. regression replay
    print("\n7. Replay of the real saved decisions (no crew re-run)\n")
    expected = {"CLM-5000": "auto_approve", "CLM-5004": "human_review", "CLM-5005": "auto_approve",
                "CLM-5006": "reject", "CLM-5007": "auto_approve", "CLM-5008": "human_review",
                "CLM-6001": "auto_approve"}
    sources = {"CLM-5000": "decisions", "CLM-5004": "decisions", "CLM-6001": "decisions"}
    for cid, want in expected.items():
        path = ROOT / "data" / sources.get(cid, "assessment") / f"{cid}.json"
        if not path.exists():
            run(f"{cid} saved decision is present", False, "runtime fixture missing on this checkout")
            continue
        saved = json.loads(path.read_text())
        claim = json.loads((ROOT / "data" / "claims" / f"{cid}.json").read_text())
        doubts = coverage_doubts({"policy_no": claim["policy_no"],
                                  "incident_date": claim["incident_date"]}, saved["decision"])
        got = decide_route(saved["decision"], saved.get("settlement") or {}, [], doubts)[0]
        run(f"{cid} routes as before: {want}", got == want, f"got {got}, doubts {doubts}")

    print(f"\n{passed}/{total} clause scope checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
