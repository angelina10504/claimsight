"""
ClaimSight - Claims Intelligence Workspace.

A local window onto what ClaimSight has recorded. Presentation mode (the
default) reads saved results and calls no model. Live mode runs the real Flow
for one development claim, only when asked, in its own process.

    uv run streamlit run dashboard/app.py --server.address 127.0.0.1
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import streamlit as st                                    # noqa: E402

import components as ui                                   # noqa: E402
import data_adapter as da                                 # noqa: E402
import flow_runner                                        # noqa: E402
from styles import CSS, INK, LINE, MUTED, TEAL            # noqa: E402

ROOT = HERE.parent
PAGES = ("Overview", "Claim Investigation", "Evidence & Recovery", "Agent Activity")
MODES = ("Presentation", "Live assessment")
DEMO_FIRST = ("CLM-5000", "CLM-5004")

st.set_page_config(page_title="ClaimSight", page_icon="◆", layout="wide",
                   initial_sidebar_state="expanded")
ui.html(CSS)


# ---------------------------------------------------------------- shared
def offline_env() -> dict:
    """The environment for a sandbox or test subprocess: offline, and with
    every provider key blank so nothing it imports can reach a model."""
    env = dict(os.environ, CLAIMSIGHT_OFFLINE="1")
    for key in ("GROQ_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
                "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        env[key] = ""
    return env


def status_badge(key: str) -> str:
    label, tone, icon = da.status_label(key)
    return ui.badge(label, tone, icon)


def claim_options() -> list:
    saved = [r.claim_id for r in da.saved_claims()]
    first = [c for c in DEMO_FIRST if c in saved] + [c for c in saved if c not in DEMO_FIRST]
    return first + [c for c in da.known_claims() if c not in first]


def describe_option(claim_id: str) -> str:
    label, _, _ = da.status_label(da.load_record(claim_id).status)
    return f"{claim_id}  ·  {label}"


def saved_label(record) -> None:
    if record.source == "flow":
        ui.banner(f"Saved assessment, recorded {ui.e(record.saved_at or 'earlier')}. "
                  "Shown as recorded: nothing here is recalculated or re-run.", "info", "◆")
    elif record.source == "assessment_only":
        ui.banner("Assessed by an earlier run_assessment.py run, outside the Flow: it has a "
                  "decision and a settlement, but was never routed or authorised.", "info", "○")


def live_run_notice(claim_id: str) -> None:
    run = flow_runner.current()
    if not run or run.get("broken") or run.get("claim_id") != claim_id:
        return
    phase = flow_runner.progress(run)["phase"]
    if phase in ("running", "timed_out"):
        ui.banner("A live run of this claim is in progress. What is shown is the saved result "
                  "from before it started; it is replaced only if the run succeeds.", "warn", "◐")
    elif phase == "failed":
        ui.banner("The latest live run of this claim failed. The decision below is the "
                  "previous saved one, not a result of that run.", "bad", "!")


# ---------------------------------------------------------------- sidebar
# A link such as ?page=Agent+Activity&claim=CLM-5004 opens there, which makes
# bookmarks for a presentation. Both values are checked against fixed lists.
if "page" not in st.session_state and st.query_params.get("page") in PAGES:
    st.session_state["page"] = st.query_params["page"]
if "claim_id" not in st.session_state and da.is_known(st.query_params.get("claim")):
    st.session_state["claim_id"] = st.query_params["claim"]

with st.sidebar:
    ui.html("<div class='cs-brand'>ClaimSight</div>"
            "<div class='cs-brand-sub'>Claims Intelligence Workspace</div>")
    st.write("")
    page = st.radio("Navigate", PAGES, key="page", label_visibility="collapsed")
    st.divider()
    mode = st.radio("Mode", MODES, key="mode",
                    help="Presentation reads saved results and calls no model. "
                         "Live assessment can run the real Flow for one claim.")
    if mode == MODES[0]:
        st.caption("Saved results only · no model calls · nothing is overwritten")
    else:
        st.caption("Live runs use Groq tokens and replace the claim's saved result. "
                   "Each run is started by hand.")
    if page in ("Claim Investigation", "Agent Activity"):
        st.divider()
        options = claim_options()
        if st.session_state.get("claim_id") not in options:
            st.session_state["claim_id"] = options[0] if options else None
        st.selectbox("Claim", options, key="claim_id", format_func=describe_option,
                     help="Type to search. Saved claims are listed first.")


# ---------------------------------------------------------------- overview
def page_overview() -> None:
    ui.html("<div class='cs-hero'><div class='cs-eyebrow'>Phase 2 · Agentic AI · CSE3101</div>"
            "<h1>ClaimSight</h1>"
            "<div class='cs-lede'>A multi-agent system that assesses motor insurance claims. "
            "Language models judge — coverage, fraud signals, which invoice lines to pay — "
            "and Python calculates: every rupee figure and every route comes from code a "
            "person can read, and a doubtful claim goes to a person.</div></div>")
    if mode == MODES[0]:
        ui.banner("Presentation mode: every figure on these pages is read from saved runs. "
                  "No model is called.", "good", "✓")

    counts, processed = da.dataset_counts(), da.overview_counts()
    ui.section("The dataset", "synthetic")
    ui.stat_row([
        (counts["total"], "official synthetic claims (data/manifest.json)", "Dataset", "plain"),
        (counts["dev"], "development claims, used while building", "Dataset", "plain"),
        (counts["held_out"], "held out for the final evaluation", "Dataset", "plain"),
    ])
    ui.section("Processed so far", "saved results")
    ui.stat_row([
        (processed["saved"], "claims with a saved assessment", "Processed", "accent"),
        (processed["approved"], "approved", "Outcome", "accent"),
        (processed["awaiting_review"], "awaiting human review", "Outcome", "warn"),
        (processed["awaiting_evidence"], "awaiting evidence", "Outcome", "warn"),
    ])
    if processed["assessed_only"]:
        ui.html(f"<div class='cs-note' style='margin-top:.5rem'>{processed['through_flow']} "
                f"went through the full Flow; {processed['assessed_only']} were assessed by an "
                f"earlier stand-alone run and never routed.</div>")

    ui.section("How a claim moves through the system")
    ui.html("""
    <div class='cs-flow'>
      <div class='cs-node'><div class='t'>Claimant</div><div class='s'>chats, asks to assess a claim</div></div>
      <div class='cs-arrow'><div class='line'>→</div>chat</div>
      <div class='cs-node'><div class='t'>Google ADK</div><div class='s'>Claimant Assistant, on Gemini</div></div>
      <div class='cs-arrow'><div class='line'>→</div>A2A</div>
      <div class='cs-node key'><div class='t'>CrewAI Flow</div><div class='s'>ClaimFlow with typed state; Intake crew reads the documents</div></div>
      <div class='cs-arrow'><div class='line'>→</div>kickoff</div>
      <div class='cs-node key'><div class='t'>Assessment</div><div class='s'>Policy Analyst ∥ Fraud Analyst, then the Assessment Lead</div></div>
      <div class='cs-arrow'><div class='line'>→</div>decision</div>
      <div class='cs-node'><div class='t'>Python router</div><div class='s'>settlement calculator, routing rules, human gate</div></div>
    </div>
    <div class='cs-mcp'><strong>MCP — shared tool access.</strong> One tool server
      (<code>src/mcp_server.py</code>) used by the CrewAI analysts (policy period, part prices,
      prior claims) and by the ADK assistant (claim status). It is how agents reach tools,
      not how agents talk to each other: that is A2A.</div>""")

    ui.section("What each technology does here")
    cards = [
        ("CrewAI", "Multi-agent orchestration: the Flow, the Intake and Assessment crews, "
                   "parallel analysts and typed hand-offs."),
        ("Google ADK", "The claimant-facing agent: a conversation that can start an assessment "
                       "and report where a claim stands."),
        ("A2A", "Agent interoperability: the ADK assistant hands a claim to the CrewAI "
                "adjudicator over the A2A protocol."),
        ("MCP", "Tool access: one server, each agent seeing only the tools it needs."),
        ("RAG", "Policy evidence: the Policy Analyst retrieves passages of the real policy "
                "wording, and each cited clause is checked against it."),
        ("Python", "Deterministic work: depreciation, GST, deductible, routing and every "
                   "check that decides money."),
    ]
    for row in (cards[:3], cards[3:]):
        cols = st.columns(3)
        for col, (title, body) in zip(cols, row):
            with col:
                ui.html(ui.card(title, body))
        st.write("")

    ui.section("Recent claims", "saved decisions")
    records = da.saved_claims()
    if not records:
        st.info("No claim has been assessed yet.")
        return
    rows = []
    for record in records[:8]:
        m = da.money(record) if record.source != "none" else {"proposed": None}
        rows.append([ui.e(record.claim_id), status_badge(record.status),
                     ui.e(da.ROUTE.get((record.state or {}).get("route"), "—")),
                     ui.e(ui.rupees(m.get("proposed"))),
                     ui.e(da.final_payment_text(record) if record.source == "flow" else "—"),
                     ui.e(record.saved_at or "—")])
    ui.html(ui.table(["Claim", "Status", "Route", "Proposed", "Final payment", "Saved"],
                     rows, numeric=(3,)))


# ---------------------------------------------------------------- investigation
def page_investigation() -> None:
    claim_id = st.session_state.get("claim_id")
    if not claim_id:
        st.info("No claims are available.")
        return
    record = da.load_record(claim_id)
    submission = da.claim_submission(claim_id) or {}
    policy = da.policy_details(submission.get("policy_no")) or {}

    head, badge_col = st.columns([3, 2])
    with head:
        ui.html(f"<div class='cs-eyebrow'>Claim investigation · {ui.e(da.split_of(claim_id))} "
                f"claim</div><h1 style='margin:0'>{ui.e(claim_id)}</h1>")
    with badge_col:
        route = da.ROUTE.get((record.state or {}).get("route"), "Not routed")
        ui.html(f"<div style='text-align:right;margin-top:1.4rem'>{status_badge(record.status)}"
                f"&nbsp;{ui.badge('Route: ' + route, 'info')}</div>")
    saved_label(record)
    live_run_notice(claim_id)
    if record.problem and record.source in ("unreadable", "none") and record.status != "not_assessed":
        ui.banner(ui.e(f"This claim's saved record is unavailable: {record.problem}."), "bad", "!")

    # A. summary
    ui.section("A · Claim summary")
    left, right = st.columns([1, 1.25])
    with left:
        vehicle = " ".join(str(policy.get(k)) for k in ("make", "model") if policy.get(k)) or "—"
        ui.html(ui.card("Claim", "") .replace("<p></p>", ui.kv([
            ("Policy", submission.get("policy_no") or "—"),
            ("Vehicle", vehicle),
            ("Registration", policy.get("registration") or "—"),
            ("Vehicle age", f"{policy['vehicle_age_years']} years"
                if policy.get("vehicle_age_years") is not None else "—"),
            ("Incident date", submission.get("incident_date") or "Not provided"),
            ("Status", status_badge(record.status), True),
            ("Final route", da.ROUTE.get((record.state or {}).get("route"), "Not routed")),
        ])))
    with right:
        narrative = ((record.state or {}).get("intake") or {}).get("narrative") \
            or submission.get("narrative") or "No narrative on file."
        ui.html(ui.card("What the claimant said", "").replace(
            "<p></p>", f"<div class='cs-quote'>{ui.e(narrative)}</div>"
            "<div class='cs-note' style='margin-top:.6rem'>As the agents read it, after input "
            "screening.</div>"))

    if record.source not in ("flow", "assessment_only"):
        st.write("")
        ui.banner("This claim has no saved assessment, so there is nothing to explain yet. "
                  "Its documents are below." + (" In Live mode it can be run from this page."
                                                if flow_runner.eligible(claim_id) else ""),
                  "neutral", "○")
        documents(claim_id, record, submission, policy)
        if mode == MODES[1]:
            live_panel(claim_id)
        return

    decision = (record.state or {}).get("decision") if record.source == "flow" \
        else record.legacy["decision"]
    decision = decision or {}

    # B. assessment overview
    ui.section("B · Assessment overview")
    coverage = str(decision.get("coverage_status") or
                   ("covered" if decision.get("is_covered") else "excluded")).lower()
    cov_tone = {"covered": "good", "excluded": "bad", "uncertain": "warn"}.get(coverage, "neutral")
    score = decision.get("fraud_score")
    flags = decision.get("fraud_flags") or []
    review_reason = record.report.route_reason if record.source == "flow" else ""
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        ui.html(ui.card("Policy coverage", ui.badge(coverage.capitalize(), cov_tone)
                        + (f"<div class='cs-note' style='margin-top:.5rem'>"
                           f"{ui.e('; '.join(decision.get('uncertainty_reasons') or []))}</div>"
                           if decision.get("uncertainty_reasons") else ""), raw_body=True))
    with c2:
        tone = "warn" if (score or 0) >= 0.5 else "good"
        ui.html(ui.card("Fraud-risk score",
                        f"<span style='font-size:1.6rem;font-weight:700;color:{INK}'>"
                        f"{ui.e(f'{score:.2f}' if score is not None else '—')}</span> "
                        f"{ui.badge('review threshold 0.50', tone)}"
                        "<div class='cs-note' style='margin-top:.4rem'>A risk score from 0 to 1, "
                        "not a probability.</div>", raw_body=True))
    with c3:
        body = "".join(f"<div style='margin-bottom:.3rem'>• {ui.e(f)}</div>" for f in flags) \
            or "None raised."
        ui.html(ui.card("Fraud indicators", body, raw_body=True))
    with c4:
        doubts = (record.state or {}).get("coverage_doubts") or []
        # plain text: card() escapes it
        if record.status == "awaiting_review":
            body = review_reason or "Sent to a person by the routing rules."
        elif doubts:
            body = doubts[0]
        else:
            body = review_reason or "Not applicable."
        ui.html(ui.card("Why it was routed this way", body))

    # C. invoice lines
    ui.section("C · Invoice and line-item assessment")
    rows = []
    for line in da.line_rows(record):
        ref = line["reference"]
        ref_text = (f"₹{ref['market_price']:,.0f}"
                    + (f" <span class='cs-chip'>{ref['ratio']}× market</span>" if ref.get("ratio") else "")
                    if ref else "<span class='cs-note'>none recorded</span>")
        rows.append([ui.e(line["description"]), ui.e(ui.rupees(line["amount"])),
                     ui.badge(line["status"], line["tone"]),
                     ui.e(line["reason"]) or "<span class='cs-note'>—</span>", ref_text])
    ui.html(ui.table(["Item", "Billed", "Assessment", "Reason", "Reference price"], rows,
                     numeric=(1,)))
    if any(r["status"].startswith("Disputed") for r in da.line_rows(record)):
        ui.html("<div class='cs-note' style='margin-top:.4rem'>A disputed line is the agents' "
                "recommendation, not a final refusal: it becomes denied only if an adjuster "
                "decides so.</div>")

    # D. settlement
    ui.section("D · Settlement breakdown", "Python calculator")
    m = da.money(record)
    left, right = st.columns([1.3, 1])
    with left:
        b = m["breakdown"]
        if b:
            ui.html("<div class='cs-card'>" + "".join([
                ui.money_row("Admitted subtotal", ui.rupees(b.get("admitted_subtotal"))),
                ui.money_row("Less depreciation", "− " + ui.rupees(b.get("total_depreciation"))),
                ui.money_row("After depreciation", ui.rupees(b.get("after_depreciation"))),
                ui.money_row("GST at 18%", "+ " + ui.rupees(b.get("gst"))),
                ui.money_row("Less deductible", "− " + ui.rupees(b.get("deductible"))),
                ui.money_row("Proposed settlement", ui.rupees(b.get("payout")), total=True),
            ]) + ("<div class='cs-note'>Capped at the insured declared value.</div>"
                  if b.get("capped_at_idv") else "") + "</div>")
        else:
            ui.html(ui.card("No settlement breakdown",
                            m["explanation"] or "Nothing was computed for this claim."))
    with right:
        ui.html(ui.payline("Proposed settlement", ui.rupees(m["proposed"]), "info"))
        final = da.final_payment_text(record)
        tone = "good" if m["state"] == "authorised" else "bad" if m["state"] == "refused" else "warn"
        ui.html(ui.payline("Final payment", final, tone))
        ui.html(f"<div class='cs-note' style='margin-top:.5rem'>{ui.e(m['label'])}</div>")

    # E. explanation
    if record.source == "flow":
        explain(record)
    documents(claim_id, record, submission, policy)
    if mode == MODES[1]:
        live_panel(claim_id)


def explain(record) -> None:
    report = record.report
    ui.section("E · Explainable decision", "from the recorded run")
    ui.html(f"<div class='cs-card'><div class='cs-quote'>{ui.e(da.explanation(record))}</div>"
            "<div class='cs-note' style='margin-top:.6rem'>Assembled by src/reporting.py from the "
            "saved state and log. No model writes this explanation.</div></div>")
    st.write("")
    left, right = st.columns(2)
    with left:
        refs = report.coverage_reasons
        body = "".join(
            f"<div style='margin-bottom:.55rem'>"
            f"{ui.badge('verified' if r.grounded else 'not verified' if r.grounded is False else 'not checked', 'good' if r.grounded else 'warn' if r.grounded is False else 'neutral')}"
            f"<div style='margin-top:.25rem;color:{INK}'>{ui.e(r.detail)}</div>"
            f"<div class='cs-note'>{ui.e(r.source)}</div></div>" for r in refs) \
            or "No policy reference was given."
        ui.html(ui.card("Policy references", body, raw_body=True))
    with right:
        tools = [r for r in report.evidence if r.kind == "tool"]
        body = "".join(
            f"<div style='margin-bottom:.45rem'><strong style='color:{INK}'>"
            f"{ui.e(r.source.split(' (')[0])}</strong>"
            f"<div class='cs-note'>{ui.e(r.source.split(' (', 1)[1].rstrip(')') if ' (' in r.source else '')}"
            f"</div></div>" for r in tools) or "No tool call was recorded."
        route_line = (f"<div style='margin-top:.5rem'><strong style='color:{INK}'>Routing</strong>"
                      f"<div class='cs-note'>{ui.e(report.route_reason or '—')}</div></div>")
        pay = (f"<div style='margin-top:.5rem'><strong style='color:{INK}'>Payment</strong>"
               f"<div class='cs-note'>{ui.e(da.final_payment_text(record))}</div></div>")
        ui.html(ui.card("Tool findings and routing", body + route_line + pay, raw_body=True))

    with st.expander("Technical evidence: every tool result, guardrail check and analyst report"):
        for ref in report.evidence:
            when = f" · {ref.at_seconds:.1f}s" if ref.at_seconds is not None else ""
            st.markdown(f"**{ref.kind}** · {ref.source}{when}")
            st.code(da.sanitise(ref.detail)[:1500], language=None)
    with st.expander("Settlement calculator's own explanation"):
        st.code(da.money(record)["explanation"] or "—", language=None)


def documents(claim_id, record, submission, policy) -> None:
    ui.section("F · Supporting documents")
    tabs = st.tabs(["Invoice", "Claim submission", "Policy schedule", "Reports"])
    with tabs[0]:
        pdf = da.invoice_pdf(claim_id)
        if not pdf:
            st.info("No invoice is on file for this claim.")
        else:
            image = render_pdf(claim_id, pdf)
            if image is not None:
                st.image(image, width=620)
            st.download_button("Download invoice PDF", pdf, file_name=f"{claim_id}-invoice.pdf",
                               mime="application/pdf", key=f"pdf-{claim_id}")
            st.caption("The invoice as submitted (synthetic). The agents receive a copy with "
                       "the policyholder's name, address and phone removed.")
    with tabs[1]:
        if submission:
            ui.html(ui.kv([("Claim", submission.get("claim_id")),
                           ("Policy", submission.get("policy_no")),
                           ("Incident date", submission.get("incident_date") or "Not provided"),
                           ("Invoice date", submission.get("invoice_date") or "—"),
                           ("Photos", submission.get("photos"))]))
        else:
            st.info("No submission is on file.")
    with tabs[2]:
        if policy:
            ui.html(ui.kv([(k.replace("_", " ").capitalize(), v) for k, v in policy.items()]))
            st.caption("Name, phone and address are not shown.")
        else:
            st.info("No policy schedule is on file.")
    with tabs[3]:
        downloads = da.report_downloads(record)
        if not downloads:
            st.info("A report exists only for a claim that has been through the Flow.")
        else:
            c1, c2 = st.columns(2)
            c1.download_button("Download report (Markdown)", downloads["markdown"],
                               file_name=f"{claim_id}-report.md", mime="text/markdown",
                               key=f"md-{claim_id}")
            c2.download_button("Download report (JSON)", downloads["json"],
                               file_name=f"{claim_id}-report.json", mime="application/json",
                               key=f"json-{claim_id}")
            st.caption("Built in memory by src/reporting.py, the same report as show_report.py.")


@st.cache_data(show_spinner=False, max_entries=16)
def render_pdf(claim_id: str, pdf: bytes):
    try:
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(pdf)
        return document[0].render(scale=1.5).to_pil()
    except Exception:                                        # noqa: BLE001
        return None


# ---------------------------------------------------------------- live mode
def _start_live(claim_id: str) -> None:
    """Runs only from the button's own click, never on a rerun."""
    reuse = st.session_state.get(f"reuse-{claim_id}", True)
    ok, message = flow_runner.start(claim_id, reuse_intake=reuse)
    st.session_state["live_message"] = (ok, message)
    st.session_state[f"confirm-{claim_id}"] = False


def live_panel(claim_id: str) -> None:
    ui.section("Live assessment", "uses Groq tokens")
    run = flow_runner.current()
    if run and not run.get("broken"):
        live_progress()
        return
    if run and run.get("broken"):
        st.warning("A previous live run left an unreadable lock.")
        st.button("Clear it", on_click=flow_runner.acknowledge, key="clear-broken")
        return
    if not flow_runner.eligible(claim_id):
        st.info("Live runs are limited to the 20 development claims. This claim is shown "
                "from saved results only.")
        return
    has_intake = da.has_saved_intake(claim_id)
    ui.banner("This runs the real ClaimFlow for this claim: Groq tokens are spent (about 40,000 "
              "for an assessment), the free tier can rate-limit it, and the claim's saved "
              "decision and log are replaced if it succeeds. It runs the CrewAI Flow directly; "
              "it is not the ADK → A2A path.", "warn", "!")
    st.checkbox("Reuse the saved intake" if has_intake else
                "Reuse the saved intake (none on file: the Intake crew will run)",
                value=has_intake, disabled=not has_intake, key=f"reuse-{claim_id}")
    st.checkbox(f"I want to run {claim_id} live now", key=f"confirm-{claim_id}")
    c1, c2 = st.columns([1, 3])
    c1.button("Run live assessment", type="primary", key=f"run-{claim_id}",
              disabled=not st.session_state.get(f"confirm-{claim_id}"),
              on_click=_start_live, args=(claim_id,))
    if c2.button("Check readiness", key=f"pre-{claim_id}"):
        ready = flow_runner.preflight()
        c2.write(("Groq key set" if ready["groq_key"] else "No Groq key found") + " · " +
                 ("Ollama reachable" if ready["ollama"] else "Ollama not reachable (needed "
                                                            "for the policy RAG)"))
    message = st.session_state.pop("live_message", None)
    if message:
        (st.success if message[0] else st.error)(message[1])


@st.fragment(run_every=3)
def live_progress() -> None:
    run = flow_runner.current()
    if not run or run.get("broken"):
        st.info("No live run.")
        return
    p = flow_runner.progress(run)
    claim_id = run["claim_id"]
    if p["phase"] == "running":
        st.info(f"Running {claim_id} live · {p['elapsed']:.0f}s · steps appear as the Flow "
                f"logs them.")
        steps = da.timeline(p["entries"])[-6:]
        ui.html("<div class='cs-tl'>" + "".join(
            ui.timeline_item(s["at"], s["title"], s["detail"], s["tone"]) for s in steps)
            + "</div>")
        if p["rate_limited"]:
            st.caption("The provider rate-limited a call; the client waits and retries.")
        st.button("Cancel run", key="cancel-run", on_click=flow_runner.cancel, args=(run,))
    elif p["phase"] == "timed_out":
        st.error(f"The run has taken longer than {flow_runner.TIMEOUT_SECONDS // 60} minutes.")
        st.button("Cancel run", key="cancel-timeout", on_click=flow_runner.cancel, args=(run,))
    elif p["phase"] == "succeeded":
        label, _, _ = da.status_label(p["status"])
        st.success(f"{claim_id} finished: {label}. The pages now show this run's result.")
        st.button("Done", key="ack-ok", on_click=flow_runner.acknowledge)
    else:
        st.error(f"The live run of {claim_id} failed: {p['reason']}. "
                 + ("The provider rate-limited it; try again after a minute, or tomorrow if the "
                    "daily allowance is spent. " if p["rate_limited"] else "")
                 + "The saved decision was not replaced.")
        c1, c2 = st.columns(2)
        c1.button("Restore the previous log and decision", key="restore",
                  on_click=flow_runner.restore, args=(run,))
        c2.button("Dismiss", key="ack-fail", on_click=flow_runner.acknowledge)
        tail = flow_runner.output_tail(run)
        if tail:
            with st.expander("Console output (last lines)"):
                st.code(tail, language=None)


# ---------------------------------------------------------------- evidence
SANDBOX = HERE / "evidence_sandbox.py"


def sandbox_dir() -> Path:
    """Where the sandbox keeps its claim; evidence_sandbox.py reads the same."""
    return Path(os.environ.get("CLAIMSIGHT_SANDBOX_DIR")
                or HERE / ".runtime" / "evidence_sandbox")


def sandbox(*args) -> dict:
    try:
        done = subprocess.run([sys.executable, str(SANDBOX), *args], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=180, env=offline_env())
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (subprocess.SubprocessError, json.JSONDecodeError, IndexError, OSError):
        return {"error": "the sandbox did not answer"}


def page_evidence() -> None:
    ui.html("<div class='cs-eyebrow'>Evidence and recovery</div>"
            "<h1 style='margin:0'>Request evidence → validate → resume → reassess</h1>")
    ui.html("<div class='cs-lede' style='margin:.5rem 0 1rem 0'>When a claim is missing "
            "something, ClaimSight does not fail it or guess. It stops, names exactly what is "
            "needed, checks what arrives with plain rules, and carries on from where it "
            "stopped, re-running only what the new evidence affects.</div>")
    ui.banner("Controlled offline demonstration: the real Flow, evidence checks and resume "
              "logic, with the two crews replaced by the fixed stand-ins from "
              "test_evidence_flow.py. No model is called, and it runs in its own workspace, so "
              "no saved claim is touched.", "info", "◆")

    # Nothing runs when the page opens: the sandbox is read or driven only by
    # a button, so a rerun never starts a process.
    state = st.session_state.get("sandbox")
    if state is None and sandbox_dir().exists():
        if st.button("Load the existing sandbox claim", key="sb-load"):
            with st.spinner("Reading the sandbox claim"):
                st.session_state["sandbox"] = sandbox("status")
            st.rerun()
    state = state or {"exists": False}

    stages = sandbox_stages(state)
    ui.steps(stages)
    st.write("")

    left, right = st.columns([1, 1.2])
    with left:
        ui.section("Drive it")
        if st.button("1 · Create the sandbox claim", key="sb-reset",
                     help="A copy of CLM-5000 with its incident date removed"):
            with st.spinner("Running the Flow until it stops"):
                st.session_state["sandbox"] = sandbox("reset")
            st.rerun()
        if state.get("exists"):
            missing = state.get("missing") or []
            item = st.selectbox("Evidence for", missing or ["incident date"], key="sb-item",
                                disabled=not missing)
            value = st.text_input("Value (try 30-05-2026 first, then 2026-05-30)",
                                  key="sb-value", max_chars=40)
            if st.button("2 · Submit evidence", key="sb-submit", disabled=not value):
                with st.spinner("Checking it"):
                    st.session_state["sandbox"] = sandbox("submit", "--item", item,
                                                          "--value", value)
                st.rerun()
            fail = st.toggle("Simulate a 429 rate limit during the reassessment", key="sb-fail")
            if st.button("3 · Resume the claim", key="sb-resume", type="primary"):
                with st.spinner("Resuming"):
                    st.session_state["sandbox"] = sandbox(
                        "resume", *(["--simulate-rate-limit"] if fail else []))
                st.rerun()
        last = state
        if last.get("error"):
            st.error(last["error"])
        elif last.get("done") == "submit":
            (st.success if last.get("accepted") else st.warning)(
                ("Accepted: " if last.get("accepted") else "Refused: ") + last.get("verdict", ""))
        elif last.get("done") == "resume":
            (st.success if last.get("resumed") else st.warning)(last.get("why", ""))

    with right:
        ui.section("Claim state")
        if not state.get("exists"):
            st.info("Create the sandbox claim to begin.")
        else:
            m_auth = state.get("authorised")
            ui.html(ui.kv([
                ("Claim", f"{state['claim_id']} (sandbox copy of CLM-5000)"),
                ("Status", status_badge(state.get("status") or ""), True),
                ("Missing", ", ".join(state.get("missing") or []) or "nothing"),
                ("Incident date", state.get("incident_date") or "not on file"),
                ("Proposed", ui.rupees(state.get("proposed"))),
                ("Final payment", ui.rupees(m_auth) + " authorised" if m_auth is not None
                    else "not authorised"),
            ]))
            ui.section("Evidence ledger")
            tones = {"pending": "warn", "processing": "info", "applied": "good",
                     "rejected": "bad", "refused": "bad"}
            rows = [[ui.e(i["item"]), ui.badge(i["state"].capitalize(), tones.get(i["state"], "neutral")),
                     ui.e(i["verdict"]), ui.e(i["attempts"]),
                     ui.e((i.get("last_error") or "")[:70]) or "<span class='cs-note'>—</span>"]
                    for i in state.get("items") or []]
            if rows:
                ui.html(ui.table(["Item", "State", "Check", "Attempts", "Last error"], rows))
            else:
                st.caption("Nothing submitted yet.")

    ui.section("Why a failure loses nothing")
    ui.html("<div class='cs-card'><p>Accepted evidence moves <strong>pending → processing → "
            "applied</strong>, and is only marked applied once the reassessment has produced a "
            "decision and saved it. If the reassessment fails — a 429 from the provider, a "
            "timeout — the intake is rolled back, no stale decision is written, the item goes "
            "back to <strong>pending</strong> with the error and the attempt recorded, and the "
            "next resume tries again. After three failed attempts it is handed to a person.</p></div>")
    st.write("")
    c1, c2 = st.columns([1, 2])
    if c1.button("Run the evidence-cycle checks now", key="sb-tests"):
        with st.spinner("Running test_evidence_flow.py offline"):
            try:
                done = subprocess.run([sys.executable, str(ROOT / "test_evidence_flow.py")],
                                      cwd=str(ROOT), capture_output=True, text=True, timeout=300,
                                      env=offline_env())
                summary = [l for l in done.stdout.splitlines() if "checks as expected" in l]
                st.session_state["sb-tests"] = summary[-1] if summary else "the checks did not finish"
            except subprocess.SubprocessError:
                st.session_state["sb-tests"] = "the checks did not finish"
    if st.session_state.get("sb-tests"):
        c2.success(st.session_state["sb-tests"] + " (offline, stand-in crews)")
    st.caption("Live version: CLM-9002, built with `test_evidence_flow.py --make-demo-claim`, "
               "runs the same cycle in the terminal with Groq for the reassessment.")


def sandbox_stages(state: dict) -> list:
    exists = state.get("exists")
    items = state.get("items") or []
    accepted = [i for i in items if i["accepted"]]
    failed = any(i.get("last_error") for i in accepted if i["state"] != "applied")
    decided = state.get("status") in ("approved", "rejected", "awaiting_review")
    submitted = bool(items)
    return [
        ("Claim received", "done" if exists else "todo"),
        ("Required information missing", "done" if exists else "todo"),
        ("Evidence requested", "done" if exists else "todo"),
        ("Evidence submitted", "done" if submitted else "now" if exists else "todo"),
        ("Validation completed", "done" if accepted else "fail" if submitted else "todo"),
        ("Assessment resumed", "done" if decided else "fail" if failed else
         "now" if accepted else "todo"),
        ("Decision generated", "done" if decided else "todo"),
    ]


# ---------------------------------------------------------------- activity
def page_activity() -> None:
    claim_id = st.session_state.get("claim_id")
    record = da.load_record(claim_id) if claim_id else None
    ui.html(f"<div class='cs-eyebrow'>Agent activity and observability</div>"
            f"<h1 style='margin:0'>{ui.e(claim_id or '')}</h1>")
    if not record or not record.log:
        st.info("No monitoring log is saved for this claim. A claim gets one when it runs "
                "through the Flow.")
        observability_notes(None)
        return
    live_run_notice(claim_id)
    facts = da.run_facts(record)
    model = facts["model"] or "Not recorded"
    ui.stat_row([
        (f"{facts['total_seconds']}s" if facts["total_seconds"] is not None else "—",
         "total run time", "Duration", "accent"),
        (f"{facts['total_tokens']:,}" if facts["total_tokens"] else "Not recorded",
         "tokens, as the provider reported", "Tokens", "accent"),
        (facts["tool_calls"] if facts["tool_calls"] is not None else "—",
         "MCP tool calls", "Tools", "plain"),
        (facts["retried_llm_failures"] if facts["retried_llm_failures"] is not None else "—",
         "model calls failed and retried", "Retries", "warn" if facts["retried_llm_failures"] else "plain"),
    ])
    ui.html(f"<div class='cs-note' style='margin-top:.5rem'>Model: <strong>{ui.e(model)}</strong>"
            + ("" if facts["model"] else " — this log was written before runs recorded their "
               "model") + (f" · run started {ui.e(facts['started_at'])}" if facts["started_at"] else "")
            + "</div>")

    spans = da.task_spans(record.log)
    if spans:
        ui.section("Agent tasks over time", "measured")
        duration_chart(spans)
        ui.html("<div class='cs-note'>From the log's timestamps. The Policy and Fraud Analysts "
                "are started together by the Flow and run in parallel.</div>")

    ui.section("Execution timeline")
    every = da.timeline(record.log)
    guards = [s for s in every if s["kind"] == "guard"]
    stopped = sum(s["tone"] != "good" for s in guards)
    show_guards = st.toggle(f"Show each guardrail check ({len(guards)} checks, "
                            + (f"{stopped} stopped an output" if stopped else "all passed") + ")",
                            value=False, key="tl-guards")
    steps = [s for s in every if show_guards or s["kind"] != "guard"]
    left, right = st.columns([1.35, 1])
    with left:
        ui.html("<div class='cs-tl'>" + "".join(
            ui.timeline_item(s["at"], s["title"], s["detail"], s["tone"]) for s in steps)
            + "</div>")
    with right:
        ui.html("<div class='cs-card'><h4>Tool calls</h4><p>Each MCP call, with what the agent "
                "asked and what the tool answered. Phone numbers, emails and ID numbers are "
                "masked.</p></div>")
        for s in [s for s in steps if s["kind"] == "tool"]:
            entry = s["entry"]
            with st.expander(f"{s['at']:.1f}s · {da.tool_name(entry.get('tool'))}"):
                st.caption("Input")
                st.code(da.sanitise(entry.get("input")), language="json")
                st.caption("Output")
                st.code(da.sanitise(entry.get("result")), language="json")
        crews = facts["crews"]
        if crews:
            st.write("")
            rows = [[ui.e(name), ui.e(f"{c['seconds']}s" if c["seconds"] is not None else "—"),
                     ui.e(f"{c['total_tokens']:,}" if c["total_tokens"] else "Not recorded"),
                     ui.e(f"{c['prompt_tokens']:,}" if c.get("prompt_tokens") else "—"),
                     ui.e(f"{c['completion_tokens']:,}" if c.get("completion_tokens") else "—")]
                    for name, c in crews.items()]
            ui.html(ui.table(["Crew", "Time", "Tokens", "Prompt", "Completion"], rows,
                             numeric=(1, 2, 3, 4)))
    observability_notes(facts)


def duration_chart(spans: list) -> None:
    import altair as alt
    import pandas as pd
    frame = pd.DataFrame(spans)
    frame["label"] = frame["seconds"].map(lambda s: f"{s:.1f}s")
    order = list(frame["agent"])
    base = alt.Chart(frame).encode(
        y=alt.Y("agent:N", sort=order, title=None, axis=alt.Axis(labelLimit=280)))
    bars = base.mark_bar(color=TEAL, cornerRadius=4, height=16).encode(
        x=alt.X("start:Q", title="seconds since the claim started"), x2="end:Q",
        tooltip=[alt.Tooltip("agent:N", title="Agent"),
                 alt.Tooltip("start:Q", title="Started (s)", format=".1f"),
                 alt.Tooltip("end:Q", title="Finished (s)", format=".1f"),
                 alt.Tooltip("seconds:Q", title="Took (s)", format=".1f")])
    text = base.mark_text(align="left", dx=6, color=INK, fontSize=12).encode(
        x="end:Q", text="label:N")
    chart = (bars + text).properties(height=46 * len(frame)).configure_view(stroke=None) \
        .configure_axis(gridColor=LINE, domain=False, labelColor=MUTED, titleColor=MUTED,
                        labelFontSize=12, titleFontSize=11, tickColor=LINE)
    st.altair_chart(chart, width="stretch")
    with st.expander("As a table"):
        st.dataframe(frame[["agent", "start", "end", "seconds"]], hide_index=True,
                     width="stretch")


def observability_notes(facts) -> None:
    ui.section("Observability")
    cols = st.columns(3)
    notes = [
        ("Local callback log", "Every claim writes data/logs/<claim>.jsonl from CrewAI's step "
                               "and task callbacks and the event bus. This page is built from "
                               "it, so it works with no network."),
        ("OpenTelemetry", "CrewAI, the LLM client and ADK all emit OpenTelemetry spans, scrubbed "
                          "of PII before export."),
        ("Langfuse", "Spans are exported to Langfuse when keys are set. ADK and CrewAI run in "
                     "separate processes in the live demo, so they appear as separate traces; "
                     "no single trace spans both yet."),
    ]
    for col, (title, body) in zip(cols, notes):
        with col:
            ui.html(ui.card(title, body))
    trace = facts.get("trace_id") if facts else None
    st.caption(f"Trace id for this run: {trace}" if trace else
               "No trace id was recorded for this run (tracing was off when it ran).")


# ---------------------------------------------------------------- route
{"Overview": page_overview, "Claim Investigation": page_investigation,
 "Evidence & Recovery": page_evidence, "Agent Activity": page_activity}[page]()
