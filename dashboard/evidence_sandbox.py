"""
The evidence cycle, run for real in an isolated workspace.

    request evidence -> validate -> resume -> reassess

Everything here is the project's own code: ClaimFlow, evidence.submit, the
completeness check, resume_claim and the reporting. Only two things are
replaced, both as test_evidence_flow.py replaces them:

    the crews      stand-ins from test_evidence_flow.py, so no model is
                   called; the Intake and Assessment results are fixed
    the folders    claims, intake, decisions, queue, logs, evidence and
                   reports all point into dashboard/.runtime/evidence_sandbox

so the official dataset, CLM-9002 and every saved claim are untouched.

It runs as its own process because it redirects module-level folders, and
inside the dashboard's process that would redirect them for every page.

    python dashboard/evidence_sandbox.py reset
    python dashboard/evidence_sandbox.py submit --item "incident date" --value 2026-05-30
    python dashboard/evidence_sandbox.py resume [--simulate-rate-limit]
    python dashboard/evidence_sandbox.py status

Each prints one JSON object on its last line.
"""

import os

os.environ["CLAIMSIGHT_OFFLINE"] = "1"
for _key in ("GROQ_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
             "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ[_key] = ""                # "" survives load_dotenv; a pop does not

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

# CLAIMSIGHT_SANDBOX_DIR lets a test use its own workspace, not the demo's.
WORKSPACE = Path(os.environ.get("CLAIMSIGHT_SANDBOX_DIR")
                 or ROOT / "dashboard" / ".runtime" / "evidence_sandbox")
CLAIM_ID = "CLM-9101"                    # a sandbox copy of CLM-5000, never official


def _redirect():
    import claim_flow
    import evidence
    import monitoring
    import reporting
    import test_evidence_flow as fixtures

    claim_flow.CLAIMS = WORKSPACE / "claims"
    claim_flow.INTAKE = WORKSPACE / "intake"
    claim_flow.DECISIONS = WORKSPACE / "decisions"
    claim_flow.REVIEW_QUEUE = WORKSPACE / "review_queue"
    monitoring.LOGS = WORKSPACE / "logs"
    evidence.EVIDENCE = WORKSPACE / "evidence"
    reporting.DECISIONS, reporting.REVIEW_QUEUE = claim_flow.DECISIONS, claim_flow.REVIEW_QUEUE
    reporting.LOGS, reporting.REPORTS = monitoring.LOGS, WORKSPACE / "reports"
    claim_flow.run_intake_step = fixtures.stand_in_intake
    claim_flow.run_assessment_step = fixtures.stand_in_assessment
    return claim_flow, evidence, reporting, fixtures


def status() -> dict:
    claim_flow, evidence, reporting, _ = _redirect()
    state = claim_flow.saved_state(CLAIM_ID)
    if not state:
        return {"exists": False}
    items = []
    for item in evidence.ledger(CLAIM_ID)["items"]:
        items.append({"item": item.get("item"), "value": item.get("value"),
                      "accepted": bool(item.get("accepted")),
                      "state": evidence.state_of(item) if item.get("accepted") else "rejected",
                      "verdict": item.get("verdict", ""), "attempts": item.get("attempts", 0),
                      "last_error": item.get("last_error"),
                      "submitted_at": item.get("submitted_at")})
    report = reporting.build_report(state)
    return {"exists": True, "claim_id": CLAIM_ID, "status": state.get("status"),
            "route": state.get("route"),
            "missing": [r.item for r in evidence.outstanding(state) if r.from_whom == "claimant"],
            "incident_date": (state.get("intake") or {}).get("incident_date"),
            "evidence_applied": state.get("evidence_applied") or [],
            "resumed": bool(state.get("resumed")),
            "proposed": report.proposed_payout if state.get("decision") else None,
            "authorised": report.authorised_payout,
            "trail": (state.get("trail") or [])[-8:],
            "items": items}


def reset() -> dict:
    """A fresh copy of CLM-5000 with no incident date, run until it stops."""
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    claim_flow, _, _, fixtures = _redirect()
    fixtures.build_claim(claim_flow.CLAIMS, CLAIM_ID, drop_date=True)
    claim_flow.ClaimFlow().kickoff(inputs={"claim_id": CLAIM_ID, "interactive": False})
    return {"done": "reset", **status()}


def submit(item: str, value: str) -> dict:
    claim_flow, evidence, _, _ = _redirect()
    state = claim_flow.saved_state(CLAIM_ID)
    if not state:
        return {"error": "create the sandbox claim first"}
    result = evidence.submit(CLAIM_ID, item, value=value, state=state)
    return {"done": "submit", "accepted": bool(result.get("accepted")),
            "verdict": result.get("verdict", ""), **status()}


def resume(simulate_rate_limit: bool) -> dict:
    claim_flow, _, _, fixtures = _redirect()
    if simulate_rate_limit:
        def rate_limited(inputs, verbose):
            raise RuntimeError("Error code: 429 - rate limit reached (simulated in the sandbox)")
        claim_flow.run_assessment_step = rate_limited
    outcome = claim_flow.resume_claim(CLAIM_ID, interactive=False)
    return {"done": "resume", "resumed": bool(outcome.get("resumed")),
            "why": outcome.get("why", ""), "simulated_failure": simulate_rate_limit,
            **status()}


def main(argv) -> dict:
    action = argv[0] if argv else "status"
    args = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1) if argv[i].startswith("--")}
    if action == "reset":
        return reset()
    if action == "submit":
        return submit(args.get("--item", ""), args.get("--value", "")[:60])
    if action == "resume":
        return resume("--simulate-rate-limit" in argv)
    return status()


if __name__ == "__main__":
    noise = io.StringIO()
    try:
        with contextlib.redirect_stdout(noise):     # the Flow prints; the page wants JSON
            result = main(sys.argv[1:])
    except Exception as exc:                        # noqa: BLE001
        result = {"error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
    print(json.dumps(result, default=str))
