"""
Test the Phase 2 evaluation harness (evaluation/evaluate_phase2.py).

Costs no tokens and calls no model: the crews are stand-ins, and every run
goes into a temporary folder. What is tested is the harness itself, so that
the real run, which takes days of the free tier, is not the first time it runs.

    A and B both go through the Flow, and share the settlement calculator
    nothing is written under data/
    every record names its model, prompt version, tool configuration and claim
    C is recorded as not available
    a stopped run carries on where it stopped, without repeating a claim
    the daily token budget stops the run before it is passed
    the provider's daily limit stops the run, and the claim is retried later
    a run will not resume on changed settings
    only development claims are evaluated
    an escalation is never counted as a correct approval or rejection
    the line-item score counts denied lines, and leaves out uncovered claims
    no money cost appears anywhere in the output

Run:  uv run python test_evaluation.py
"""

import os
# No offline test sends anything to Langfuse: without this, a test that
# starts the A2A server would pick up the real keys from .env and post
# spans, which is how an automated run came to report a 401.
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

import csv
import io
import json
import string
import sys
import tempfile
import warnings
from contextlib import redirect_stdout
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evaluation"))

import evaluate_phase2 as ep                         # noqa: E402
import monitoring                                    # noqa: E402
from tools.assessment_tools import settle            # noqa: E402

DATA_FOLDERS = ("decisions", "review_queue", "logs", "intake")


def data_listing() -> dict:
    out = {}
    for name in DATA_FOLDERS:
        folder = ROOT / "data" / name
        out[name] = sorted((p.name, p.stat().st_mtime) for p in folder.glob("*")) \
            if folder.exists() else []
    return out


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = fn(*args, **kwargs)
    return result, out.getvalue()


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" +
              (f"\n        {detail}" if detail else ""))

    ep.RUNS = Path(tempfile.mkdtemp(prefix="claimsight_eval_"))
    before = data_listing()

    # ---------------------------------------------------------------- a whole run
    print("1. A full rehearsal over the development claims\n")
    rehearsal = ep.Run("rehearsal", stand_in=True)
    session, _ = quiet(ep.evaluate, rehearsal, daily_tokens=0)
    a, b = rehearsal.records("A"), rehearsal.records("B")
    run("all 20 development claims ran through A and B", len(a) == 20 and len(b) == 20,
        f"A {len(a)}, B {len(b)}, failed {len(session['failed'])}")
    run("nothing is left to run", session["remaining"] == 0 and not session["stopped"])
    run("nothing was written under data/", data_listing() == before)

    every = a + b
    needed = ("claim_id", "model", "prompt_version", "tool_configuration",
              "intake_prompt_version", "decision_code", "stand_in", "started_at")
    run("every record names its claim, model, prompt version and tool configuration",
        all(all(r.get(k) not in (None, "") for k in needed if k != "stand_in") for r in every))
    run("a stand-in record says so, and claims no model",
        all(r["stand_in"] is True and "stand-in" in r["model"] for r in every))
    run("A and B have different prompt versions and tool configurations",
        a[0]["prompt_version"] != b[0]["prompt_version"]
        and a[0]["tool_configuration"] != b[0]["tool_configuration"])
    run("and the same decision code, which is what makes them comparable",
        a[0]["decision_code"] == b[0]["decision_code"])

    # The calculator is shared: each payout is exactly what settle() gives for
    # that configuration's own line decisions.
    mismatched = []
    for config in ("A", "B"):
        for path in sorted(rehearsal.path(config).glob("*/CLM-*.json")):
            if path.parent.name not in ("decisions", "review_queue"):
                continue
            state = json.loads(path.read_text())
            decision = state.get("decision") or {}
            if decision.get("coverage_status") == "excluded" or not decision:
                continue
            expected = settle(state["intake"]["policy_no"], decision["line_decisions"])["payout"]
            if abs(expected - state["settlement"]["payout"]) > 0.005:
                mismatched.append((config, path.stem))
    run("every payout is the settlement calculator's, for both configurations",
        not mismatched, str(mismatched))
    same = [(x, y) for x, y in zip(a, b)
            if [l["admitted"] for l in x["line_decisions"]] ==
               [l["admitted"] for l in y["line_decisions"]]
            and x["coverage_status"] == y["coverage_status"]]
    run("the same line decisions give the same payout in A and B",
        same and all(x["proposed_payout"] == y["proposed_payout"] for x, y in same),
        f"{len(same)} claim(s) decided alike")

    intake = json.dumps([json.loads(p.read_text())
                         for p in rehearsal.path("intake").glob("CLM-????.json")])
    run("the shared intake carries no label from the dataset",
        "ground_truth" not in intake and '"admitted"' not in intake)
    run("the intake ran once per claim, for A, and B reused it",
        all(not r["intake_reused"] for r in a) and all(r["intake_reused"] for r in b))

    # ---------------------------------------------------------------- the report
    print("\n2. The report\n")
    summary = ep.report(rehearsal)
    paths = ep.write_outputs(rehearsal, summary)
    text = ep.table(summary)
    run("C is recorded as not available, with the reason",
        summary["configs"]["C"] == {"available": False, "why": ep.NOT_AVAILABLE["C"]}
        and "not avail." in text)
    run("the table says plainly that a rehearsal measures nothing",
        text.startswith("STAND-IN REHEARSAL"))
    run("JSON, CSV and a summary CSV are written",
        all(p.exists() for p in paths.values()))
    rows = list(csv.DictReader(paths["csv"].open()))
    run("the CSV has one row per claim and configuration", len(rows) == 40, f"{len(rows)}")
    run("and each row names its model, prompt version and claim",
        all(r["model"] and r["prompt_version"] and r["claim_id"] for r in rows))
    blob = (paths["json"].read_text() + paths["csv"].read_text()
            + paths["summary_csv"].read_text()).lower()
    run("no money cost is estimated anywhere",
        not any(word in blob for word in ("cost_usd", "usd", "dollar", "cost_inr", "$")))
    run("tokens are counted as the stand-ins reported them",
        summary["configs"]["B"]["assessment_total_tokens_mean"] == ep.STAND_IN_USAGE["B"]
        and summary["intake"]["total_tokens_mean"] == ep.STAND_IN_USAGE["intake"]
        and summary["configs"]["A"]["end_to_end_tokens_mean"]
        == ep.STAND_IN_USAGE["A"] + ep.STAND_IN_USAGE["intake"])
    b_scores = summary["configs"]["B"]
    run("B's stand-in escalates the 2 inflated and 2 duplicate claims, and only those",
        b_scores["outcomes"]["escalated"] == 4
        and b_scores["escalation_by_fault_type"]["inflated_amount"]["escalated"] == 2
        and b_scores["escalation_by_fault_type"]["duplicate_claim"]["escalated"] == 2
        and b_scores["escalation_by_fault_type"]["clean"]["escalated"] == 0,
        json.dumps(b_scores["escalation_by_fault_type"]))
    run("and none of the four is counted as correct",
        b_scores["outcomes"]["correct_approve"] + b_scores["outcomes"]["correct_reject"] == 16)

    # ---------------------------------------------------------------- resuming
    print("\n3. A run carries on where it stopped\n")
    again, _ = quiet(ep.evaluate, rehearsal, daily_tokens=0)
    run("running the finished run again runs nothing", not again["ran"] and not again["failed"])
    run("and the records are untouched",
        [r["started_at"] for r in rehearsal.records("B")] == [r["started_at"] for r in b])

    partial = ep.Run("partial", stand_in=True)
    first, _ = quiet(ep.evaluate, partial, max_units=5, daily_tokens=0)
    run("a run can be stopped after a number of claims",
        len(first["ran"]) == 5 and "stopped after 5" in (first["stopped"] or ""),
        first["stopped"])
    run("A and B take turns, so a stopped run compares them on the same claims",
        [(r["config"], r["claim_id"]) for r in first["ran"]][:4] ==
        [("A", "CLM-5000"), ("B", "CLM-5000"), ("A", "CLM-5001"), ("B", "CLM-5001")])
    halfway = ep.report(partial)
    run("a report on a part-finished run compares only the claims both have finished",
        halfway["paired_claims"] == ["CLM-5000", "CLM-5001"]
        and halfway["unpaired"]["A"] == ["CLM-5002"]
        and halfway["configs"]["A"]["claims"] == 2, json.dumps(halfway["unpaired"]))
    rest, _ = quiet(ep.evaluate, partial, daily_tokens=0)
    run("the next run finishes the rest without repeating any",
        len(rest["ran"]) == 35 and rest["remaining"] == 0, f"{len(rest['ran'])}")

    # ---------------------------------------------------------------- the budget
    print("\n4. The daily token budget\n")
    budget = ep.Run("budget", stand_in=True)
    limit = 45_000
    result, _ = quiet(ep.evaluate, budget, daily_tokens=limit)
    spent = budget.spent_today("none (stand-in)")
    run("the run stops by itself before the budget is passed",
        result["stopped"] and "today's budget" in result["stopped"] and spent <= limit,
        f"spent {spent:,} of {limit:,}; {result['stopped']}")
    run("having done some of the work, and left the rest",
        result["ran"] and result["remaining"] > 0,
        f"{len(result['ran'])} done, {result['remaining']} left")
    run("tokens are entered in a ledger per day and model, which survives the run",
        budget.read("ledger.json", default={}).get(ep._today(), {}).get("none (stand-in)") == spent)

    # ---------------------------------------------------------------- failures
    print("\n5. The provider's daily limit, and other failures\n")
    limited = ep.Run("limited", stand_in=True)

    def hits_daily_limit(inputs, verbose):
        monitoring.current().record("llm_call_failed", kind="daily_token_limit",
                                    error="Rate limit reached ... tokens per day (TPD)")
        raise RuntimeError("Assessment crew returned no valid AssessmentDecision")

    broken = {"B": (ep.stand_in_intake, hits_daily_limit)}
    result, _ = quiet(ep.evaluate, limited, steps=broken, daily_tokens=0)
    run("the daily limit stops the whole run, not just the claim",
        result["stopped"] and "daily token limit" in result["stopped"]
        and len(result["ran"]) == 1, result["stopped"])
    error = limited.read("B", "CLM-5000.error.json", default={})
    run("the failure is saved with its cause, and the claim is not marked done",
        error.get("llm_errors") == ["daily_token_limit"] and not limited.done("B", "CLM-5000"),
        json.dumps(error)[:160])
    retried, _ = quiet(ep.evaluate, limited, daily_tokens=0)
    run("the next run retries it and clears the failure",
        limited.done("B", "CLM-5000")
        and not limited.path("B", "CLM-5000.error.json").exists()
        and retried["remaining"] == 0)

    def always_fails(inputs, verbose):
        raise RuntimeError("Ollama is not running")

    failing = ep.Run("failing", stand_in=True)
    result, _ = quiet(ep.evaluate, failing, steps={"A": (ep.stand_in_intake, always_fails),
                                                   "B": (ep.stand_in_intake, always_fails)},
                      daily_tokens=0)
    run(f"{ep.FAILURES_IN_A_ROW} failures in a row stop the run rather than go through "
        f"every claim", len(result["failed"]) == ep.FAILURES_IN_A_ROW
        and "in a row" in (result["stopped"] or ""), result["stopped"])

    # ---------------------------------------------------------------- settings
    print("\n6. Reproducibility\n")
    original = ep.SINGLE_LLM_PROMPT
    ep.SINGLE_LLM_PROMPT = original + "\nBe brief."
    try:
        refused = False
        try:
            rehearsal.check_settings(("A", "B"))
        except SystemExit as stop:
            refused = "prompt_version" in str(stop)
        run("a run will not resume once A's prompt has changed", refused)
        rehearsal.check_settings(("A", "B"), allow_changes=True)
        run("unless told to, knowingly", True)
    finally:
        ep.SINGLE_LLM_PROMPT = original
        rehearsal.check_settings(("A", "B"), allow_changes=True)
    run("the settings each configuration ran with are kept in run.json",
        set(rehearsal.read("run.json")["configs"]) == {"A", "B"})

    print("\n7. Only development claims\n")
    for claim_id, why in (("CLM-5020", "a held-out claim"), ("CLM-9001", "a demo fixture")):
        try:
            ep.select_claims([claim_id])
            refused = False
        except SystemExit:
            refused = True
        run(f"{why} ({claim_id}) is refused", refused)
    run("by default the 20 development claims, from the manifest",
        [e["claim_id"] for e in ep.select_claims()] ==
        [f"CLM-50{n:02d}" for n in range(20)])

    # ---------------------------------------------------------------- scoring
    print("\n8. Scoring\n")
    truths = {"CLM-1": {"fault_type": "clean", "is_covered": True, "is_fraudulent": False,
                        "expected_payout": 1000.0,
                        "lines": [{"description": "Bumper", "admitted": True},
                                  {"description": "Labour", "admitted": True}]},
              "CLM-2": {"fault_type": "date_outside_policy", "is_covered": False,
                        "is_fraudulent": False, "expected_payout": 0.0,
                        "lines": [{"description": "Bumper", "admitted": True}]},
              "CLM-3": {"fault_type": "inflated_amount", "is_covered": True,
                        "is_fraudulent": True, "expected_payout": 500.0,
                        "lines": [{"description": "Door", "admitted": False},
                                  {"description": "Paint", "admitted": True}]}}

    def record(claim_id, route, status="covered", fraud=0.1, lines=(), payout=None):
        return {"claim_id": claim_id, "route": route, "coverage_status": status,
                "fraud_score": fraud, "seconds": 1.0, "proposed_payout": payout,
                "line_decisions": [{"description": d, "admitted": a} for d, a in lines],
                "tokens": {"assessment": None}}

    escalated_clean = ep.score([record("CLM-1", "human_review",
                                       lines=[("Bumper", True), ("Labour", True)],
                                       payout=1000.0)], truths)
    run("a clean claim sent to a person is an escalation, not a correct approval",
        escalated_clean["outcomes"]["escalated"] == 1
        and escalated_clean["outcomes"]["correct_approve"] == 0
        and escalated_clean["correct_decision_rate"] == 0.0
        and escalated_clean["accuracy_when_decided"] is None)
    run("an escalation is not a wrong decision either",
        escalated_clean["outcomes"]["wrong_approve"] == 0
        and escalated_clean["outcomes"]["wrong_reject"] == 0)

    uncertain = ep.score([record("CLM-1", "human_review", status="uncertain")], truths)
    run("'uncertain' coverage is its own category, neither right nor wrong",
        uncertain["coverage"] == {"correct": 0, "wrong": 0, "uncertain": 1, "not_assessed": 0}
        and uncertain["coverage_accuracy"] == 0.0
        and uncertain["coverage_accuracy_when_judged"] is None)

    excluded = ep.score([record("CLM-2", "reject", status="excluded",
                                lines=[("Bumper", False)], payout=0.0)], truths)
    run("a correct rejection is counted as one",
        excluded["outcomes"]["correct_reject"] == 1 and excluded["coverage_accuracy"] == 1.0)
    run("and its refused lines are not scored as wrongly denied",
        excluded["line_items"]["fp"] == 0 and excluded["line_f1"] is None)

    lines = ep.score([record("CLM-3", "human_review", fraud=0.7,
                             lines=[("Door", False), ("Paint", True)], payout=500.0),
                      record("CLM-1", "auto_approve", lines=[("Bumper", True)],
                             payout=900.0)], truths)
    run("a line left out of the decision counts as denied, since it is not paid",
        lines["line_items"] == {"tp": 1, "fp": 1, "fn": 0, "tn": 2, "unmatched": 0},
        json.dumps(lines["line_items"]))
    run("line F1 is computed on denied lines", lines["line_f1"] == 0.6667, lines["line_f1"])
    run("fraud is a score at or above the Flow's threshold",
        lines["fraud"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 1}
        and lines["fraud_precision"] == 1.0 and lines["fraud_recall"] == 1.0)
    run("payout error is in rupees, against the label",
        lines["payout_mae"] == 50.0 and lines["payout_within_1"] == 1)

    # ---------------------------------------------------------------- config A
    print("\n9. Config A is given B's evidence, and nothing it cannot use\n")
    import claim_flow
    intake = json.loads(rehearsal.path("intake", "CLM-5008.json").read_text())
    inputs = claim_flow.assessment_inputs(intake)
    evidence = ep.tool_evidence(inputs)
    fields = {name for _, name, _, _ in string.Formatter().parse(ep.SINGLE_LLM_PROMPT) if name}
    run("every field in A's prompt is supplied, so a real run cannot fail on it",
        fields <= set(inputs) | set(evidence), str(sorted(fields - set(inputs) - set(evidence))))
    run("A sees the duplicate B's Fraud Analyst would find",
        json.loads(evidence["prior_claims"])["possible_duplicate"] is True)
    seen = {}

    def capture(inputs, verbose, build=None):
        seen.update(inputs=inputs, build=build)
        return {}

    real = ep.REAL_ASSESSMENT_STEP
    ep.REAL_ASSESSMENT_STEP = capture
    try:
        ep.single_llm_step(inputs, False)
    finally:
        ep.REAL_ASSESSMENT_STEP = real
    run("A goes through the Flow's own assessment step, with its single-LLM crew",
        seen.get("build") is ep.build_single_llm_crew
        and "period_check" in seen.get("inputs", {}))
    run("A's prompt never mentions a label",
        not any(w in ep.SINGLE_LLM_PROMPT.lower() for w in ("ground_truth", "fault_type",
                                                            "expected_payout")))

    print("\n10. A stand-in run and a real one are never mixed\n")
    refused = False
    try:
        quiet(ep.main, ["--run", "rehearsal", "--report"])
    except SystemExit as stop:
        refused = "stand-in" in str(stop)
    run("a stand-in run cannot be continued as a real one", refused)

    print("\n11. Nothing in the offline tests reaches the network\n")
    run("this test declares itself offline", "CLAIMSIGHT_OFFLINE" in Path(__file__).read_text())
    run("and the rehearsal sets it for itself",
        "CLAIMSIGHT_OFFLINE" in (ROOT / "evaluation" / "evaluate_phase2.py").read_text())

    print(f"\n{passed}/{total} evaluation checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
