"""
Test the Streamlit dashboard (dashboard/app.py) without spending a token.

Saved-results pages are rendered with Streamlit's AppTest, in this process,
with subprocess blocked, so any attempt to start the Flow or a model would
fail the test. The data adapter is tested on copies of the saved claims in a
temporary folder, including broken files. Live mode is tested with a fake
process. The evidence sandbox runs for real, offline, in its own folder.

    every page renders, and imports no agent framework
    a pending claim never shows a final payment of Rs 0.00
    a disputed line is not shown as denied until a person decides
    missing, malformed and old-format files are handled, not crashed on
    old logs without a model say "Not recorded"
    no label from the dataset, and no personal field, reaches the page
    only valid claim ids are read, and only development claims run live
    a live run starts only from its button, once, and never on a rerun
    the evidence sandbox refuses, accepts, survives a 429 and recovers

Run:  uv run python test_dashboard.py
"""

import os
# No offline test sends anything to Langfuse: without this, a test that
# starts the A2A server would pick up the real keys from .env and post
# spans, which is how an automated run came to report a 401.
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

import json
import shutil
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "dashboard"))

import data_adapter as da                     # noqa: E402
import flow_runner                            # noqa: E402
import reporting                              # noqa: E402

LABELS = ("ground_truth", "fault_type", "inflated_amount", "phantom_part", "duplicate_claim",
          "excluded_wear_tear", "date_outside_policy", "expected_payout", "is_fraudulent")
HEAVY = ("crewai", "claim_flow", "litellm", "google.adk", "openai")


def heavy_modules() -> list:
    return sorted({m for m in sys.modules if any(m == h or m.startswith(h + ".") for h in HEAVY)})


def page_text(at) -> str:
    parts = [m.value for m in at.markdown] + [str(x.value) for x in at.caption] + \
            [str(x.value) for x in at.info] + [str(x.value) for x in at.success] + \
            [str(x.value) for x in at.warning] + [str(x.value) for x in at.error]
    return " ".join(parts)


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" +
              (f"\n        {detail}" if detail else ""))

    from streamlit.testing.v1 import AppTest
    real_popen, real_run = subprocess.Popen, subprocess.run
    calls = []

    def blocked(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        raise AssertionError("a process was started")

    # ---------------------------------------------------------------- 1. pages
    print("1. Presentation mode renders saved results and starts nothing\n")
    before = set(heavy_modules())
    subprocess.Popen, subprocess.run = blocked, blocked
    try:
        at = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=90)
        at.run()
        errors = [str(x.value)[:200] for x in at.exception]
        run("the overview renders", not errors, str(errors))
        overview = page_text(at)
        run("it separates the dataset from what has been processed",
            "official synthetic claims" in overview and "claims with a saved assessment" in overview)
        run("it says no model is called", "No model is called" in overview)

        seen = {}
        for page in ("Claim Investigation", "Agent Activity"):
            for claim in ("CLM-5000", "CLM-5004", "CLM-5005", "CLM-5010", "CLM-5020"):
                at.sidebar.radio(key="page").set_value(page).run()
                at.sidebar.selectbox(key="claim_id").set_value(claim).run()
                seen[(page, claim)] = (page_text(at), [str(x.value)[:160] for x in at.exception])
        at.sidebar.radio(key="page").set_value("Evidence & Recovery").run()
        seen[("Evidence", "")] = (page_text(at), [str(x.value)[:160] for x in at.exception])
    finally:
        subprocess.Popen, subprocess.run = real_popen, real_run

    failures = {k: v[1] for k, v in seen.items() if v[1]}
    run("every page renders for an approved, a pending, an older, an unassessed and a "
        "held-out claim", not failures, json.dumps({f"{k[0]}/{k[1]}": v for k, v in failures.items()})[:300])
    run("no process was started while presenting", not calls, str(calls)[:200])
    run("no agent framework or model client was imported",
        set(heavy_modules()) == before, str(sorted(set(heavy_modules()) - before)))

    pending = seen[("Claim Investigation", "CLM-5004")][0]
    run("a pending claim shows its proposed settlement",
        "₹16,081.97" in pending and "Proposed settlement" in pending)
    run("and its final payment as pending human review, not ₹0.00",
        "Pending human review" in pending and "₹0.00 authorised" not in pending)
    run("the overpriced windshield is disputed, not denied",
        "Disputed, awaiting a decision" in pending and "Denied by adjuster" not in pending)
    run("the fraud score is labelled a risk score, not a probability",
        "not a probability" in pending)
    run("the routing reason is escaped once, not twice", "&amp;gt;" not in pending)
    approved = seen[("Claim Investigation", "CLM-5000")][0]
    run("an approved claim shows its authorised payment",
        "₹41,869.40 authorised" in approved)
    unassessed = seen[("Claim Investigation", "CLM-5010")][0]
    run("a claim with no saved decision says so, rather than inventing one",
        "no saved assessment" in unassessed)
    every = " ".join(v[0] for v in seen.values()) + overview
    leaked = [w for w in LABELS if w in every]
    run("no label from the dataset appears on any page", not leaked, str(leaked))
    policy = json.loads((ROOT / "data" / "policies" / "POL-3004.json").read_text())
    run("no policyholder name, phone or address appears",
        not any(str(policy[k]) in every for k in ("insured_name", "phone", "address")))
    activity = seen[("Agent Activity", "CLM-5004")][0]
    run("an old log without a model says 'Not recorded'", "Not recorded" in activity)
    run("the timeline names the MCP tool calls", "check_part_prices" in activity)
    run("and does not claim one trace spans ADK and CrewAI",
        "no single trace spans both" in activity)

    # ---------------------------------------------------------------- 2. adapter
    print("\n2. The data adapter, on copies of the saved claims\n")
    tmp = Path(tempfile.mkdtemp(prefix="claimsight_dash_"))
    data = tmp / "data"
    for folder in ("claims", "policies", "invoices"):
        shutil.copytree(ROOT / "data" / folder, data / folder)
    shutil.copy(ROOT / "data" / "manifest.json", data / "manifest.json")
    for folder in ("decisions", "review_queue", "logs", "assessment", "evidence"):
        (data / folder).mkdir(parents=True, exist_ok=True)
    saved = {"DATA": da.DATA, "DEC": reporting.DECISIONS, "RQ": reporting.REVIEW_QUEUE,
             "LOGS": reporting.LOGS}
    da.DATA = data
    reporting.DECISIONS, reporting.REVIEW_QUEUE = data / "decisions", data / "review_queue"
    reporting.LOGS = data / "logs"
    import evidence
    saved_evidence, evidence.EVIDENCE = evidence.EVIDENCE, data / "evidence"
    try:
        real5004 = json.loads((ROOT / "data" / "decisions" / "CLM-5004.json").read_text())
        (data / "decisions" / "CLM-5004.json").write_text(json.dumps(real5004))
        shutil.copy(ROOT / "data" / "logs" / "CLM-5004.jsonl", data / "logs" / "CLM-5004.jsonl")

        record = da.load_record("CLM-5004")
        money = da.money(record)
        run("a queued claim has a proposed figure and no authorised one",
            money["proposed"] == real5004["settlement"]["payout"] and money["authorised"] is None)
        run("its final payment reads as pending", da.final_payment_text(record) == "Pending human review")
        rows = {r["description"]: r["status"] for r in da.line_rows(record)}
        run("its refused line is disputed while it waits",
            rows.get("Windshield - replacement") == "Disputed, awaiting a decision", str(rows))

        decided = dict(real5004, status="rejected_by_adjuster", final_payout=0.0)
        (data / "decisions" / "CLM-5004.json").write_text(json.dumps(decided))
        record = da.load_record("CLM-5004")
        rows = {r["description"]: r["status"] for r in da.line_rows(record)}
        run("once a person has refused the claim, no line reads as payable",
            rows.get("Windshield - replacement") == "Not paid" and
            da.final_payment_text(record) == "Refused: nothing payable", str(rows))

        waiting = dict(real5004, status="awaiting_evidence", route="needs_evidence",
                       decision={}, settlement={})
        (data / "decisions" / "CLM-5004.json").write_text(json.dumps(waiting))
        record = da.load_record("CLM-5004")
        run("a claim waiting for evidence has no figure at all",
            da.money(record)["proposed"] is None and da.money(record)["authorised"] is None
            and da.final_payment_text(record) == "Pending: waiting for evidence")

        (data / "decisions" / "CLM-5004.json").write_text("{ not json")
        record = da.load_record("CLM-5004")
        run("a malformed decision file is reported as unavailable, not raised",
            record.status == "unavailable" and record.source == "unreadable")
        run("without saying where the file lives",
            str(tmp) not in (record.problem or "") and "/" not in (record.problem or ""))

        log = data / "logs" / "CLM-5004.jsonl"
        log.write_text(log.read_text() + "\n{ broken line\n\n")
        entries = reporting.load_log("CLM-5004")
        run("a malformed log line is skipped, the rest still read", len(entries) > 10)
        facts = da.run_facts(da.ClaimRecord("CLM-5004", "flow", "awaiting_review", log=entries))
        run("an old log has no model, and none is guessed", facts["model"] is None)
        steps = da.timeline(entries)
        run("its timeline says 'Not recorded' for the model",
            any(s["detail"] == "Model: Not recorded" for s in steps))
        modern = [{"event": "run_started", "elapsed_s": 0.0, "model": "openai/openai/gpt-oss-20b",
                   "provider": "groq"}]
        run("a new log's model is shown as recorded",
            da.run_facts(da.ClaimRecord("CLM-1", "flow", "x", log=modern))["model"]
            == "openai/openai/gpt-oss-20b")

        (data / "decisions" / "CLM-5004.json").unlink()
        log.unlink()
        record = da.load_record("CLM-5004")
        run("a claim with nothing saved is 'not yet assessed'",
            record.status == "not_assessed" and da.line_rows(record) == [])

        legacy = {"decision": real5004["decision"], "settlement": real5004["settlement"],
                  "score": {"secret": "a comparison with the labels"}}
        (data / "assessment" / "CLM-5004.json").write_text(json.dumps(legacy))
        record = da.load_record("CLM-5004")
        run("an older run_assessment.py result is read as assessed outside the Flow",
            record.source == "assessment_only" and da.money(record)["authorised"] is None)
        run("and its score against the labels is not carried over",
            "score" not in record.legacy and "a comparison with the labels" not in
            json.dumps(record.legacy))

        (data / "claims" / "CLM-9999.json").write_text("{}")
        run("a stray file in the claims folder is not a claim the dashboard opens",
            "CLM-9999" not in da.known_claims() and not da.is_known("CLM-9999"))
        run("the manifest gives 60 claims, 20 development and 40 held out",
            da.dataset_counts() == {"total": 60, "dev": 20, "held_out": 40})
        run("and carries no fault type", all("fault_type" not in e for e in da.manifest()))
        sub = da.claim_submission("CLM-5020")
        run("a held-out claim's submission carries no label",
            sub and not any(w in json.dumps(sub) for w in LABELS), str(sub)[:120])
        pol = da.policy_details("POL-3004")
        run("policy details drop the name, phone and address",
            pol and not set(da.PII_FIELDS) & set(pol))
        bad = ["../../etc/passwd", "CLM-5000.json", "CLM-50000", "clm-5000", "", None, 5000]
        run("anything that is not a claim id is refused",
            not any(da.is_known(b) for b in bad)
            and all(da.load_record(b).status == "unavailable" for b in bad if b is not None))
        run("so is a bad policy number", da.policy_details("../POL-3004") is None)
    finally:
        da.DATA = saved["DATA"]
        reporting.DECISIONS, reporting.REVIEW_QUEUE = saved["DEC"], saved["RQ"]
        reporting.LOGS = saved["LOGS"]
        evidence.EVIDENCE = saved_evidence
    # Imported only now, after the no-framework check: it loads CrewAI.
    from crews.intake_crew import PII_FIELDS
    run("the hidden policy fields are the ones the Intake crew drops",
        tuple(PII_FIELDS) == da.PII_FIELDS)

    # ---------------------------------------------------------------- 3. live
    print("\n3. Live mode starts a run only when asked, and only once\n")
    started = []

    class FakeProcess:
        pid = os.getpid()                       # alive for as long as the test is

        def __init__(self, args, **kwargs):
            started.append({"args": args, **kwargs})

    runtime = tmp / "runtime"
    old_runtime, old_popen, old_data = flow_runner.RUNTIME, flow_runner.Popen, flow_runner._data
    flow_runner.RUNTIME, flow_runner.Popen = runtime, FakeProcess
    live_data = tmp / "live"
    for folder in ("decisions", "review_queue", "logs"):
        (live_data / folder).mkdir(parents=True)
    (live_data / "decisions" / "CLM-5001.json").write_text('{"before": true}')
    flow_runner._data = lambda: {"decisions": live_data / "decisions",
                                 "review_queue": live_data / "review_queue",
                                 "logs": live_data / "logs",
                                 "manifest": ROOT / "data" / "manifest.json"}
    try:
        for claim, why in (("CLM-5020", "a held-out claim"), ("CLM-9002", "a demo fixture"),
                           ("../CLM-5000", "a path"), ("CLM-5000; rm -rf /", "a command")):
            ok, _ = flow_runner.start(claim)
            run(f"{why} cannot be run live", not ok and not started)

        at = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=90)
        at.run()
        at.sidebar.radio(key="page").set_value("Claim Investigation").run()
        at.sidebar.selectbox(key="claim_id").set_value("CLM-5001").run()
        run("in presentation mode there is no live button", not [b for b in at.button
                                                                if b.key == "run-CLM-5001"])
        at.sidebar.radio(key="mode").set_value("Live assessment").run()
        for _ in range(3):
            at.run()
        run("live mode, rendered and rerun, starts nothing by itself", not started)
        button = next(b for b in at.button if b.key == "run-CLM-5001")
        run("the run button is disabled until it is confirmed", button.disabled)
        at.checkbox(key="confirm-CLM-5001").check().run()
        next(b for b in at.button if b.key == "run-CLM-5001").click().run()
        run("one click starts exactly one run", len(started) == 1, str(len(started)))
        args = started[0]["args"] if started else []
        # No saved intake for CLM-5001, so the Intake crew must run: --fresh.
        expected = [str(ROOT / "run_flow.py"), "CLM-5001", "--no-input"] + \
            ([] if da.has_saved_intake("CLM-5001") else ["--fresh"])
        run("with a fixed command and no shell",
            isinstance(args, list) and args[0] == sys.executable and args[1:] == expected
            and not started[0].get("shell"), str(args[1:]))
        for _ in range(3):
            at.run()
        run("reruns while it runs start nothing more", len(started) == 1)
        run("the page shows the run in progress instead of the button",
            not [b for b in at.button if b.key == "run-CLM-5001"]
            and "Running CLM-5001 live" in page_text(at))
        ok, message = flow_runner.start("CLM-5002")
        run("a second run, even of another claim, is refused while one is going",
            not ok and len(started) == 1, message)
        run("the claim's saved decision was copied aside first",
            any(p.name == "CLM-5001.json" for p in (runtime / "backups").rglob("*.json")))
        flow_runner.acknowledge()
        run("a running run cannot be dismissed", flow_runner.current() is not None)

        run_info = flow_runner.current()
        run_info["pid"] = 999999                 # the process has gone
        progress = flow_runner.progress(run_info)
        run("a run that ended without a decision is a failure, not a success",
            progress["phase"] == "failed", progress["reason"])
        (live_data / "decisions" / "CLM-5001.json").write_text('{"half": "written"}')
        run("after a failure the previous decision can be put back",
            flow_runner.restore(run_info)
            and json.loads((live_data / "decisions" / "CLM-5001.json").read_text()) == {"before": True})
        (runtime / "live_run.json").unlink()
        (runtime / "o.txt").write_text("using key gsk_abcdefghijklmnop for the call")
        tail = flow_runner.output_tail({"output": "o.txt"})
        run("output shown from a run has keys masked",
            "[key hidden]" in tail and "gsk_abcdefghijklmnop" not in tail, tail)
    finally:
        flow_runner.RUNTIME, flow_runner.Popen, flow_runner._data = old_runtime, old_popen, old_data

    # ---------------------------------------------------------------- 4. sandbox
    print("\n4. The evidence sandbox, run for real and offline\n")
    os.environ["CLAIMSIGHT_SANDBOX_DIR"] = str(tmp / "sandbox")
    try:
        at = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=240)
        at.run()
        at.sidebar.radio(key="page").set_value("Evidence & Recovery").run()
        at.button(key="sb-reset").click().run()
        state = at.session_state["sandbox"]
        run("the claim stops and names what is missing",
            state.get("status") == "awaiting_evidence" and state.get("missing") == ["incident date"],
            str({k: state.get(k) for k in ("status", "missing", "error")}))
        at.text_input(key="sb-value").set_value("30-05-2026").run()
        at.button(key="sb-submit").click().run()
        state = at.session_state["sandbox"]
        run("a badly written date is refused, with the reason",
            state.get("accepted") is False and "YYYY-MM-DD" in state.get("verdict", ""))
        at.text_input(key="sb-value").set_value("2026-05-30").run()
        at.button(key="sb-submit").click().run()
        state = at.session_state["sandbox"]
        run("a good date is accepted and waits as pending",
            state.get("accepted") is True
            and [i["state"] for i in state["items"] if i["accepted"]] == ["pending"])
        at.toggle(key="sb-fail").set_value(True).run()
        at.button(key="sb-resume").click().run()
        state = at.session_state["sandbox"]
        item = [i for i in state["items"] if i["accepted"]][0]
        run("a simulated 429 leaves the claim waiting, not decided",
            state.get("resumed") is False and state.get("status") == "awaiting_evidence")
        run("and the evidence pending, with the attempt and the error recorded",
            item["state"] == "pending" and item["attempts"] == 1 and "429" in item["last_error"])
        at.toggle(key="sb-fail").set_value(False).run()
        at.button(key="sb-resume").click().run()
        state = at.session_state["sandbox"]
        item = [i for i in state["items"] if i["accepted"]][0]
        run("the retry decides the claim and applies the evidence",
            state.get("status") == "approved" and item["state"] == "applied"
            and item["attempts"] == 2)
        run("the page shows the applied evidence", "Applied" in page_text(at))
        run("no saved claim was touched: the sandbox kept to its own folder",
            (tmp / "sandbox" / "decisions").exists()
            and not (ROOT / "data" / "decisions" / "CLM-9101.json").exists())
    finally:
        os.environ.pop("CLAIMSIGHT_SANDBOX_DIR", None)

    print("\n5. Local only\n")
    config = (ROOT / ".streamlit" / "config.toml").read_text()
    run("the server listens on 127.0.0.1 only", 'address = "127.0.0.1"' in config)
    run("and sends no usage statistics", "gatherUsageStats = false" in config)
    run("this test declares itself offline", "CLAIMSIGHT_OFFLINE" in Path(__file__).read_text())

    print(f"\n{passed}/{total} dashboard checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
