"""
Test the Streamlit dashboard (dashboard/app.py) without spending a token.

Saved-results pages are rendered with Streamlit's AppTest, in this process,
with subprocess blocked, so any attempt to start the Flow or a model would
fail the test. The data adapter is tested on copies of the saved claims in a
temporary folder, including broken files. Live mode is tested with a fake
process. The evidence sandbox runs for real, offline, in its own folder.

    every page renders, and imports no agent framework
    the walkthrough shows each agent ran or skipped, and why, from the log
    the current flow stage is labelled, and a paused claim draws the evidence loop
    a billed part is flagged only when the photograph shows it intact
    held-out claims cannot be picked
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


def ui_escape(text: str) -> str:
    from html import escape
    return escape(text or "")


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
        at.sidebar.radio(key="page").set_value("Claim Walkthrough").run()
        options = " ".join(at.sidebar.selectbox(key="claim_id").options)
        run("held-out claims are not in the picker; development and photo claims are",
            "CLM-5020" not in options and "CLM-5059" not in options
            and "CLM-5000" in options and "CLM-6001" in options, options[:120])
        for page in ("Claim Walkthrough", "Agent Activity"):
            for claim in ("CLM-5000", "CLM-5004", "CLM-5005", "CLM-5010", "CLM-6001"):
                at.sidebar.radio(key="page").set_value(page).run()
                at.sidebar.selectbox(key="claim_id").set_value(claim).run()
                seen[(page, claim)] = (page_text(at), [str(x.value)[:160] for x in at.exception])
        at.sidebar.radio(key="page").set_value("Evidence & Recovery").run()
        seen[("Evidence", "")] = (page_text(at), [str(x.value)[:160] for x in at.exception])
    finally:
        subprocess.Popen, subprocess.run = real_popen, real_run

    failures = {k: v[1] for k, v in seen.items() if v[1]}
    run("every page renders for an approved, a pending, an older, an unassessed and a "
        "photo claim", not failures, json.dumps({f"{k[0]}/{k[1]}": v for k, v in failures.items()})[:300])
    run("no process was started while presenting", not calls, str(calls)[:200])
    run("no agent framework or model client was imported",
        set(heavy_modules()) == before, str(sorted(set(heavy_modules()) - before)))

    pending = seen[("Claim Walkthrough", "CLM-5004")][0]
    run("a pending claim shows its proposed settlement",
        "₹16,081.97" in pending and "Proposed settlement" in pending)
    run("and its final payment as pending human review, not ₹0.00",
        "Pending human review" in pending and "₹0.00 authorised" not in pending)
    run("the overpriced windshield is disputed, not denied",
        "Disputed, awaiting a decision" in pending and "Denied by adjuster" not in pending)
    run("the walkthrough says the intake agents were skipped, and why",
        "intake saved by an earlier run was reused" in pending)
    run("and that the Damage Analyst was skipped for want of a photograph",
        "No photograph on this claim, so the Damage Analyst is skipped" in pending)
    # Expected values come from the saved run itself, so re-running the claim
    # does not break the test.
    log5004 = reporting.load_log("CLM-5004")
    span = next(s for s in da.task_spans(log5004) if "Policy Analyst" in s["agent"])
    crew = next(e for e in log5004 if e.get("event") == "crew_done"
                and e.get("crew") == "assessment")
    reason = reporting.build_report(reporting.load_state("CLM-5004"), log5004).route_reason
    run("the analysts are shown as run, with their MCP tools and timings",
        f"Ran · {span['seconds']:.1f}s" in pending and "check_policy_period" in pending
        and "find_prior_claims" in pending)
    run("tokens are given per crew, not invented per agent",
        f"{int(crew['total_tokens']):,} tokens for the whole crew" in pending)
    run("the current stage is named in words", "▶ Current · Done" in pending
        and "human_review because" in pending)
    run("the decision shows route, reason, proposed payout and settlement state",
        all(t in pending for t in ("Route: Human review", ui_escape(reason),
                                   "Proposed payout", "Settlement state",
                                   reporting.SETTLEMENT_STATE["pending_human_review"])),
        reason)
    run("the routing reason is escaped once, not twice", "&amp;gt;" not in pending)
    approved = seen[("Claim Walkthrough", "CLM-5000")][0]
    run("an approved claim shows its authorised payment",
        "₹41,869.40 authorised" in approved)
    run("and every assessment agent as run, with the Damage Analyst skipped",
        approved.count("Ran · ") == 5 and "Damage Analyst is skipped" in approved)
    run("a verified policy citation is marked verified", "verified" in approved)
    unassessed = seen[("Claim Walkthrough", "CLM-5010")][0]
    run("a claim with no saved decision says so, rather than inventing one",
        "No decision is saved for this claim" in unassessed
        and "No monitoring log is saved" in unassessed)
    photo = seen[("Claim Walkthrough", "CLM-6001")][0]
    state6001 = reporting.load_state("CLM-6001") or {}
    if (state6001.get("intake") or {}).get("photo_damage"):
        run("a photo claim shows its photograph beside the Damage Analyst's three lists",
            "05bf64c441a8b9f6e0b9.jpg" in photo and "Undamaged: seen clearly, intact" in photo
            and "Not visible: not evidence either way" in photo)
        run("and says whether the Flow acted on the photo check",
            ("this is what the route acted on" in photo) == bool(state6001.get("photo_check"))
            and ("did not affect the route" in photo) == (not state6001.get("photo_check")))
    else:
        run("a photo claim shows its photograph and says plainly no findings are saved yet",
            "05bf64c441a8b9f6e0b9.jpg" in photo and "Nothing is recorded yet" in photo)
    every = " ".join(v[0] for v in seen.values()) + overview
    leaked = [w for w in LABELS if w in every]
    run("no label from the dataset appears on any page", not leaked, str(leaked))
    policy = json.loads((ROOT / "data" / "policies" / "POL-3004.json").read_text())
    run("no policyholder name, phone or address appears",
        not any(str(policy[k]) in every for k in ("insured_name", "phone", "address")))
    activity = seen[("Agent Activity", "CLM-5004")][0]
    run("an old log without a model says 'Not recorded'",
        "Not recorded" in seen[("Agent Activity", "CLM-5000")][0])
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
        # An old-format log: written before runs recorded their model.
        old = [json.loads(l) for l in (ROOT / "data" / "logs" / "CLM-5004.jsonl")
               .read_text().splitlines() if l.strip()]
        for entry in old:
            entry.pop("model", None)
            entry.pop("provider", None)
        (data / "logs" / "CLM-5004.jsonl").write_text(
            "\n".join(json.dumps(e) for e in old) + "\n")

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
        sub = da.claim_submission("CLM-5005")
        run("a claim's submission carries no label",
            sub and not any(w in json.dumps(sub) for w in LABELS), str(sub)[:120])
        pol = da.policy_details("POL-3004")
        run("policy details drop the name, phone and address",
            pol and not set(da.PII_FIELDS) & set(pol))
        bad = ["../../etc/passwd", "CLM-5000.json", "CLM-50000", "clm-5000", "", None, 5000]
        run("anything that is not a claim id is refused",
            not any(da.is_known(b) for b in bad)
            and all(da.load_record(b).status == "unavailable" for b in bad if b is not None))
        run("so is a bad policy number", da.policy_details("../POL-3004") is None)
        run("a held-out claim cannot be opened at all",
            "CLM-5020" not in da.known_claims() and da.claim_submission("CLM-5020") is None)

        # ---- the walkthrough's readers
        shutil.copy(ROOT / "data" / "photo_manifest.json", data / "photo_manifest.json")
        run("photo claims are known, without their fault type",
            da.is_known("CLM-6001") and all("fault_type" not in e for e in da.photo_manifest()))
        run("a photograph missing from the folder is reported, not read",
            da.claim_photos("CLM-6001") == [{"name": "05bf64c441a8b9f6e0b9.jpg",
                                             "available": False}]
            and da.photo_bytes("CLM-6001", "05bf64c441a8b9f6e0b9.jpg") is None)
        run("a photo name that is not the claim's own is refused",
            da.photo_bytes("CLM-6001", "../manifest.json") is None)
        intake = {"invoice": {"line_items": [
                      {"description": "Fender - replacement", "amount": 6800},
                      {"description": "Front Door - replacement", "amount": 14000},
                      {"description": "Rear Bumper - replacement", "amount": 8500}]},
                  "photo_damage": [{"photo": "05bf64c441a8b9f6e0b9.jpg", "damaged": ["fender"],
                                    "undamaged": ["front door"],
                                    "not_visible": ["rear bumper"], "readable": True,
                                    "damage_types": ["tear"], "severity": "moderate",
                                    "notes": "call 9876543210"}]}
        photo_record = da.ClaimRecord("CLM-6001", "flow", "approved", state={"intake": intake})
        found = da.damage_findings(photo_record)
        run("the Damage Analyst's findings come from the intake record",
            found["source"] == "the intake record" and found["photos"][0]["damaged"] == ["fender"])
        run("with personal details masked in its note",
            "9876543210" not in found["photos"][0]["notes"], found["photos"][0]["notes"])
        check = da.photo_comparison(photo_record, found["photos"][0])
        run("a billed part shown undamaged is flagged; one not visible is only unchecked",
            check["unsupported_parts"] == ["front door"]
            and check["unchecked_parts"] == ["rear bumper"], str(check.get("note")))

        paused = [{"event": "run_started", "elapsed_s": 0.0},
                  {"event": "flow", "message": "intake: running Intake crew"},
                  {"event": "flow", "message": "intake: 3 invoice lines, complete=False"},
                  {"event": "flow", "message": "router 1: file incomplete -> evidence request "
                                                "['incident date']"},
                  {"event": "flow", "message": "evidence request: incident date"},
                  {"event": "run_finished", "status": "awaiting_evidence"}]
        flow = da.flow_stages(paused, {"status": "awaiting_evidence"})
        states = {s["key"]: s["state"] for s in flow["stages"]}
        run("a claim paused for evidence draws the loop as current, assessment skipped",
            flow["current"] == "loop" and flow["loop"]["state"] == "now"
            and "incident date" in flow["loop"]["detail"]
            and states["assessment"] == "skipped" and states["gate"] == "done", str(states))
        stopped = da.agent_walkthrough(da.ClaimRecord("CLM-5000", "flow", "awaiting_evidence",
                                                      log=paused))
        run("and its assessment agents say why they did not run",
            all("completeness check" in a["reason"] for a in stopped if a["crew"] == "assessment"))
        running = da.flow_stages(paused[:3] + [{"event": "flow", "message": "router 1: file "
                                                "complete -> assessment"},
                                               {"event": "flow", "message": "assessment: Policy "
                                                "Analyst and Fraud Analyst running in parallel"}])
        run("a run in progress highlights the stage it is in",
            running["current"] == "assessment")
        failed = da.flow_stages(paused[:3] + [{"event": "error", "where": "intake",
                                               "error_type": "RateLimitError"}])
        run("a failed run marks the stage it stopped at",
            [s["state"] for s in failed["stages"]][1] == "fail" or
            any(s["state"] == "fail" for s in failed["stages"]))
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
        at.sidebar.radio(key="page").set_value("Claim Walkthrough").run()
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
