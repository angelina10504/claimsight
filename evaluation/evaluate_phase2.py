"""
Requirement 10: end-to-end evaluation of Phase 2, with time and tokens.

Three configurations over the 20 development claims:

    A  one LLM, given the same evidence as ClaimSight, returning the same
       AssessmentDecision
    B  ClaimSight as it stands: Policy Analyst and Fraud Analyst in parallel,
       then the Assessment Lead, tools over MCP, task guardrails
    C  ClaimSight with the Damage Analyst reading the photos. That is Phase 3,
       so it is recorded as not available and the table keeps its column

What is held equal, so that what is measured is the assessment:

    the intake     run once per claim and shared, so A and B assess the same
                   extracted invoice. Its tokens are reported on their own.
    the evidence   A cannot call tools, so it is given the results of the
                   three tools B's analysts call, and reads the policy wording
                   through the same Knowledge settings
    what follows   both run through the same ClaimFlow, which checks the
                   coverage reasons and the invoice's tax, computes the payout
                   with the Python settlement calculator and routes the claim.
                   A differs from B only in the function the Flow calls to
                   assess.

No money figure is produced for the run itself. Tokens are the counts the
provider reported, as src/monitoring.py records them.

Groq's free tier allows about 200,000 tokens a day per model, and B costs about
40,000 a claim, so a run is spread over days. Each claim is saved as it
finishes; the same command run again carries on where it stopped, and stops by
itself before the day's allowance (--daily-tokens) would be passed.

Run, which costs tokens:
    uv run python evaluation/evaluate_phase2.py --run phase2-dev
Report on what is saved so far, with no model call:
    uv run python evaluation/evaluate_phase2.py --run phase2-dev --report
Rehearse with stand-in crews, which costs nothing and measures nothing:
    uv run python evaluation/evaluate_phase2.py --run rehearsal --stand-in
"""

import argparse
import contextlib
import csv
import functools
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

if "--stand-in" in sys.argv:
    os.environ["CLAIMSIGHT_OFFLINE"] = "1"   # a rehearsal sends nothing anywhere

import observability                         # noqa: E402
observability.start()                        # before the crews are imported

import claim_flow                            # noqa: E402
import monitoring                            # noqa: E402
from models import AssessmentDecision        # noqa: E402
from tools.assessment_tools import (         # noqa: E402
    _part_name,
    check_part_prices,
    check_policy_period,
    find_prior_claims,
)

RUNS = ROOT / "evaluation" / "runs"
MANIFEST = ROOT / "data" / "manifest.json"
CONFIGS = ("A", "B", "C")
AVAILABLE = ("A", "B")
NOT_AVAILABLE = {"C": "ClaimSight + vision is Phase 3; the Damage Analyst does not exist yet"}
DEFAULT_DAILY_TOKENS = 180_000      # under Groq's 200,000, for the calls retried on a 429
FIRST_ESTIMATE = 40_000             # tokens for a unit before any has finished
FAILURES_IN_A_ROW = 3               # then something is wrong that a retry will not fix

# The Flow's own steps, kept before anything is swapped in, so config A can
# call the real assessment step with its own crew.
REAL_INTAKE_STEP = claim_flow.run_intake_step
REAL_ASSESSMENT_STEP = claim_flow.run_assessment_step


class StopRun(Exception):
    """The run cannot go on today. Everything finished so far is saved."""


# ---------------------------------------------------------------- config A
SINGLE_LLM_ROLE = "Motor Claims Assessor"
SINGLE_LLM_GOAL = ("Decide coverage, weigh any sign of fraud and decide every invoice "
                   "line of a motor claim, on your own.")
SINGLE_LLM_BACKSTORY = (
    "You assess motor claims alone, from the policy wording and the claim system's checks. "
    "You never answer from general knowledge: if the wording does not say it, you say it is "
    "not stated. You do not calculate payouts; the settlement system does that from your "
    "line decisions.")

# The instructions B's three tasks give, in one prompt. Kept here rather than
# drawn from assessment_crew.py so a change to B's prompts cannot silently
# change the baseline; prompt_version fingerprints this text either way.
SINGLE_LLM_PROMPT = (
    "Assess claim {claim_id}.\n\n"
    "Incident date: {incident_date}\n"
    "What happened: {narrative}\n"
    "Parts claimed: {damaged_parts}\n"
    "Policy schedule:\n{policy}\n"
    "Invoice lines:\n{line_items}\n\n"
    "The claim system has already run these checks for you:\n"
    "Policy period (check_policy_period): {period_check}\n"
    "Prices (check_part_prices): {price_check}\n"
    "Earlier claims on this policy (find_prior_claims): {prior_claims}\n\n"
    "Coverage. Using the policy wording, decide whether the cause described is an insured "
    "peril under Section I. Collisions, impacts, being hit by another vehicle, reversing into "
    "an object and stone damage are all 'accidental external means' (Section I, item 6). "
    "Check every invoice line against the exclusions: wear and tear, mechanical or electrical "
    "breakdown, consequential loss, and tyres unless the vehicle was damaged at the same time. "
    "Replacement of body parts damaged in the accident is not wear and tear.\n\n"
    "Fraud. Consider the price check, the earlier claims, and whether the parts billed fit "
    "what the claimant says happened. Most claims are honest; flag only what the evidence "
    "shows.\n\n"
    "Apply these rules in order:\n"
    "1. Set coverage_status to exactly one of three values:\n"
    "   'excluded' ONLY if the incident is outside the policy period, or a clause of the "
    "policy rules the cause out. Then admit no lines and use route = reject.\n"
    "   A refusal is checked against the policy document itself, so a coverage_reason must "
    "either name a clause that really exists in this policy (its sections are 'Section I' "
    "and 'Section II', its exclusions are under 'GENERAL EXCEPTIONS', and its endorsements "
    "are numbered 'IMT. n') or quote the wording exactly, in quotes. Do not cite a clause "
    "that lists what the policy COVERS as a reason to refuse: quote the exclusion that "
    "applies.\n"
    "   'uncertain' if the wording does not settle it either way, or the evidence disagrees "
    "with itself. Then fill uncertainty_reasons with what could not be established and what "
    "would settle it, admit the lines you would admit if it is covered, and use route = "
    "human_review. Never guess between covered and excluded to avoid saying uncertain.\n"
    "   'covered' if the cause is an insured peril inside the policy period.\n"
    "   Do not set is_covered; it follows coverage_status.\n"
    "2. If the same parts were already claimed earlier: admit NO lines, each with the reason "
    "'same damage already claimed', fraud_score = 0.9.\n"
    "3. A line the price check flagged as overpriced is not admitted (reason: priced far "
    "above market rate); fraud_score at least 0.7.\n"
    "4. A line an exclusion applies to is not admitted, with the clause as the reason. An "
    "exclusion on its own is not fraud.\n"
    "5. Every other line is admitted, including painting and labour.\n"
    "6. With nothing suspicious, fraud_score is between 0.0 and 0.2.\n"
    "7. route: reject if coverage_status is excluded; human_review if coverage_status is "
    "uncertain or fraud_score is 0.5 or more; otherwise auto_approve.\n\n"
    "line_decisions must contain EVERY invoice line, with its description, amount and "
    "material exactly as given above. Do not calculate any payout."
)


def build_single_llm_crew(verbose: bool = False, monitor=None):
    """Config A: one agent, one task, no tools, the same structured output."""
    from crewai import Agent, Crew, Process, Task
    from crewai.knowledge.knowledge_config import KnowledgeConfig
    from crewai.knowledge.source.text_file_knowledge_source import TextFileKnowledgeSource

    import guardrails
    from config import get_llm, knowledge_limits, max_rpm
    from crews.assessment_crew import EMBEDDER

    guardrails.install()       # the same PII redaction on every call as B
    assessor = Agent(
        role=SINGLE_LLM_ROLE, goal=SINGLE_LLM_GOAL, backstory=SINGLE_LLM_BACKSTORY,
        llm=get_llm("text"),
        knowledge_sources=[TextFileKnowledgeSource(
            file_paths=["policy_wording.txt"], chunk_size=1200, chunk_overlap=200)],
        embedder=EMBEDDER,
        knowledge_config=KnowledgeConfig(**knowledge_limits()),
        verbose=verbose, allow_delegation=False)
    task = Task(description=SINGLE_LLM_PROMPT,
                expected_output="An AssessmentDecision covering every invoice line.",
                agent=assessor, output_pydantic=AssessmentDecision)
    return Crew(agents=[assessor], tasks=[task], process=Process.sequential,
                verbose=verbose, max_rpm=max_rpm(), **monitoring.crew_callbacks(monitor))


def tool_evidence(inputs: dict) -> dict:
    """The answers B's analysts get from their tools, for A's prompt."""
    lines = json.loads(inputs["line_items"])
    return {
        "period_check": json.dumps(check_policy_period(inputs["policy_no"],
                                                       inputs["incident_date"])),
        "price_check": json.dumps(check_part_prices(lines)),
        "prior_claims": json.dumps(find_prior_claims(
            inputs["policy_no"], inputs["claim_id"], inputs["incident_date"],
            json.loads(inputs["damaged_parts"]))),
    }


def single_llm_step(inputs: dict, verbose: bool) -> dict:
    return REAL_ASSESSMENT_STEP({**inputs, **tool_evidence(inputs)}, verbose,
                                build=build_single_llm_crew)


# ---------------------------------------------------------------- stand-ins
# For rehearsing the harness at no cost. They read only what a crew would,
# never the labels, and decide by fixed rules: their "results" say nothing
# about either configuration. Their token counts are made up and fixed, so the
# counting can be checked; every record they produce is marked stand_in.
STAND_IN_USAGE = {"intake": 500, "A": 1000, "B": 3000}


def _stand_in_usage(total: int) -> dict:
    return {"total_tokens": total, "prompt_tokens": total - total // 10,
            "completion_tokens": total // 10, "cached_prompt_tokens": 0,
            "reasoning_tokens": 0, "successful_requests": 1}


def stand_in_intake(claim_id: str, verbose: bool) -> dict:
    claim = json.loads((claim_flow.CLAIMS / f"{claim_id}.json").read_text())
    policy = json.loads((claim_flow.POLICIES / f"{claim['policy_no']}.json").read_text())
    lines = [{k: item[k] for k in ("description", "amount", "material")}
             for item in claim["line_items"]]
    monitor = monitoring.current()
    if monitor:
        monitor.crew_usage["intake"] = _stand_in_usage(STAND_IN_USAGE["intake"])
    return {"claim_id": claim_id, "policy_no": claim["policy_no"],
            "registration": policy["registration"],
            "incident_date": claim["incident_date"], "narrative": claim["narrative"],
            "damaged_parts": sorted({p for p in map(_part_name, (l["description"] for l in lines))
                                     if p}),
            "invoice": {"invoice_no": f"INV-{claim_id}",
                        "invoice_date": claim.get("invoice_date"),
                        "registration": policy["registration"], "line_items": lines,
                        "subtotal": claim.get("invoice_subtotal"),
                        "total": claim.get("invoice_total")},
            "is_complete": True, "missing_fields": [], "notes": "stand-in intake"}


def stand_in_assessment(inputs: dict, verbose: bool, config: str = "B") -> dict:
    """B's stand-in uses all three checks; A's skips the earlier-claims one, so
    the two differ and the comparison has something to show."""
    lines = json.loads(inputs["line_items"])
    period = check_policy_period(inputs["policy_no"], inputs["incident_date"])
    prices = check_part_prices(lines)
    prior = (find_prior_claims(inputs["policy_no"], inputs["claim_id"],
                               inputs["incident_date"], json.loads(inputs["damaged_parts"]))
             if config == "B" else {})
    overpriced = {line["description"] for line in prices.get("lines", [])
                  if line.get("check") == "OVERPRICED"}
    outside = period.get("within_period") is False
    duplicate = bool(prior.get("possible_duplicate"))

    decisions = []
    for line in lines:
        if outside:
            admitted, reason = False, "incident outside the policy period"
        elif duplicate:
            admitted, reason = False, "same damage already claimed"
        elif line["description"] in overpriced:
            admitted, reason = False, "priced far above market rate"
        else:
            admitted, reason = True, ""
        decisions.append({**line, "admitted": admitted, "reason": reason})
    score = 0.9 if duplicate else 0.7 if overpriced else 0.1
    decision = AssessmentDecision(
        claim_id=inputs["claim_id"],
        coverage_status="excluded" if outside else "covered",
        coverage_reasons=[period.get("note", "")] if outside else
                         ["Section I: loss of or damage to the vehicle insured"],
        fraud_score=score,
        fraud_flags=(["same parts already claimed"] if duplicate else []) +
                    [f"{d} overpriced" for d in sorted(overpriced)],
        line_decisions=decisions,
        recommended_route="reject" if outside else
                          "human_review" if score >= 0.5 else "auto_approve",
        summary="stand-in decision")
    monitor = monitoring.current()
    if monitor:
        monitor.record("crew_done", crew="assessment", seconds=0.0,
                       **_stand_in_usage(STAND_IN_USAGE[config]))
    return decision.model_dump()


def steps_for(config: str, stand_in: bool):
    """(intake step, assessment step) the Flow is given for this configuration."""
    if stand_in:
        return stand_in_intake, functools.partial(stand_in_assessment, config=config)
    return REAL_INTAKE_STEP, {"A": single_llm_step, "B": REAL_ASSESSMENT_STEP}[config]


# ---------------------------------------------------------------- reproducibility
def _fingerprint(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode())
    return digest.hexdigest()[:12]


def _source(*relative: str) -> list:
    return [(SRC / r).read_text() for r in relative]


def git_version() -> str:
    try:
        return subprocess.run(["git", "describe", "--always", "--dirty"], cwd=ROOT,
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def settings_for(config: str, stand_in: bool) -> dict:
    """Everything that decides what a configuration does. A run refuses to
    resume if any of it has changed, so one table never mixes two setups.

    prompt_version fingerprints the text the model is given; for B its prompts
    are in the crew files. decision_code fingerprints the code that turns a
    decision into a route and a payout, which both configurations share."""
    import config as llm_config
    shared = {
        "stand_in": stand_in,
        "provider": "none (stand-in)" if stand_in else llm_config.PROVIDER,
        "model": "none (stand-in)" if stand_in else llm_config.PROFILES[llm_config.PROVIDER]["text"],
        "temperature": 0.1,
        "llm_retries": int(os.getenv("CLAIMSIGHT_LLM_RETRIES", "5")),
        "max_rpm": llm_config.max_rpm(),
        "knowledge": llm_config.knowledge_limits(),
        "intake_prompt_version": _fingerprint(*_source("crews/intake_crew.py", "models.py")),
        "decision_code": _fingerprint(*_source("claim_flow.py", "tools/settlement.py",
                                               "tools/assessment_tools.py", "citations.py")),
        "fraud_threshold": claim_flow.FRAUD_THRESHOLD,
        "high_value": claim_flow.HIGH_VALUE,
    }
    if config == "A":
        return {**shared, "config": "A",
                "description": "single LLM, same evidence, same schema",
                "prompt_version": _fingerprint(SINGLE_LLM_ROLE, SINGLE_LLM_GOAL,
                                               SINGLE_LLM_BACKSTORY, SINGLE_LLM_PROMPT,
                                               *_source("models.py")),
                "tool_configuration": (
                    "no tool calls; results of check_policy_period, check_part_prices and "
                    "find_prior_claims given in the prompt; policy wording through "
                    "Knowledge; no task guardrails")}
    mcp = os.getenv("USE_MCP", "true").lower() != "false"
    return {**shared, "config": "B",
            "description": "ClaimSight Phase 2 architecture",
            "prompt_version": _fingerprint(*_source("crews/assessment_crew.py", "models.py",
                                                    "guardrails.py")),
            "tool_configuration": (
                f"tools {'over MCP (./claimsight_tools)' if mcp else 'called directly'}: "
                "Policy Analyst check_policy_period; Fraud Analyst check_part_prices, "
                "find_prior_claims; policy wording through Knowledge; task guardrails on "
                "all three tasks")}


# ---------------------------------------------------------------- the data
def development_claims() -> list:
    """The 20 development claims, from the manifest. Never the held-out 40,
    and never a demonstration fixture."""
    return [e for e in json.loads(MANIFEST.read_text()) if e["split"] == "dev"]


def select_claims(wanted: list = None) -> list:
    dev = development_claims()
    if not wanted:
        return dev
    by_id = {e["claim_id"]: e for e in dev}
    refused = [c for c in wanted if c not in by_id]
    if refused:
        raise SystemExit(f"not development claims, so not evaluated here: {', '.join(refused)}")
    return [by_id[c] for c in wanted]


def truth_for(claim_id: str) -> dict:
    """The labels, read only to score. Nothing here reaches a crew."""
    claim = json.loads((claim_flow.CLAIMS / f"{claim_id}.json").read_text())
    return {**claim["ground_truth"],
            "lines": [{"description": i["description"], "admitted": i["admitted"]}
                      for i in claim["line_items"]]}


# ---------------------------------------------------------------- one claim
class Run:
    """One evaluation run on disk:

        run.json                  settings per configuration, fixed at the start
        ledger.json               tokens spent per UTC day and model
        intake/<claim>.json       the shared intake, and <claim>.usage.json
        A/<claim>.json            a finished claim; B/... likewise
        A/<claim>.error.json      the last failure, retried on the next run
        A/logs, A/decisions, ...  the Flow's own files, kept out of data/
    """

    def __init__(self, name: str, stand_in: bool, root: Path = None):
        self.dir = (root or RUNS) / name
        self.stand_in = stand_in

    def path(self, *parts) -> Path:
        return self.dir.joinpath(*parts)

    def read(self, *parts, default=None):
        path = self.path(*parts)
        return json.loads(path.read_text()) if path.exists() else default

    def write(self, data, *parts) -> None:
        path = self.path(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(path)                 # a record is whole or absent

    def check_settings(self, configs, allow_changes: bool = False) -> dict:
        now = {c: settings_for(c, self.stand_in) for c in configs}
        saved = self.read("run.json", default={}).get("configs", {})
        for config, current in now.items():
            before = saved.get(config)
            if before is None or before == current or allow_changes:
                continue
            changed = sorted(k for k in set(before) | set(current)
                             if before.get(k) != current.get(k))
            raise SystemExit(
                f"run '{self.dir.name}' was started with different settings for config "
                f"{config} ({', '.join(changed)}). Start a new --run, or pass "
                f"--allow-changes to carry on knowing the table will mix the two.")
        merged = {**saved, **now}
        self.write({"created": self.read("run.json", default={}).get("created") or _now(),
                    "stand_in": self.stand_in, "configs": merged,
                    "not_available": NOT_AVAILABLE}, "run.json")
        return now

    def done(self, config: str, claim_id: str) -> bool:
        return self.path(config, f"{claim_id}.json").exists()

    # ---------------------------------------------------------- the ledger
    def spent_today(self, model: str) -> int:
        return self.read("ledger.json", default={}).get(_today(), {}).get(model, 0)

    def spend(self, model: str, tokens: int) -> None:
        ledger = self.read("ledger.json", default={})
        day = ledger.setdefault(_today(), {})
        day[model] = day.get(model, 0) + int(tokens or 0)
        self.write(ledger, "ledger.json")

    def estimate(self, config: str, claim_id: str) -> int:
        """Tokens the next unit is likely to take, from the ones finished."""
        totals = [r["tokens"]["assessment"].get("total_tokens", 0)
                  for r in self.records(config) if r["tokens"].get("assessment")]
        guess = round(statistics.mean(totals)) if totals else FIRST_ESTIMATE
        if not self.path("intake", f"{claim_id}.json").exists():
            intakes = [json.loads(p.read_text()).get("total_tokens", 0)
                       for p in self.path("intake").glob("*.usage.json")]
            guess += round(statistics.mean(intakes)) if intakes else 0
        return guess

    def records(self, config: str) -> list:
        folder = self.path(config)
        if not folder.exists():
            return []
        return [json.loads(p.read_text()) for p in sorted(folder.glob("CLM-*.json"))
                if not p.name.endswith(".error.json")]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _log_entries(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@contextlib.contextmanager
def flow_redirected(run: Run, config: str, intake_step, assessment_step):
    """Point the Flow at this run's folders and steps, and put everything back
    afterwards, whatever happens. Nothing is written under data/."""
    saved = (claim_flow.INTAKE, claim_flow.DECISIONS, claim_flow.REVIEW_QUEUE,
             monitoring.LOGS, claim_flow.run_intake_step, claim_flow.run_assessment_step)
    claim_flow.INTAKE = run.path("intake")
    claim_flow.DECISIONS = run.path(config, "decisions")
    claim_flow.REVIEW_QUEUE = run.path(config, "review_queue")
    monitoring.LOGS = run.path(config, "logs")
    claim_flow.run_intake_step = intake_step
    claim_flow.run_assessment_step = assessment_step
    try:
        yield
    finally:
        (claim_flow.INTAKE, claim_flow.DECISIONS, claim_flow.REVIEW_QUEUE,
         monitoring.LOGS, claim_flow.run_intake_step, claim_flow.run_assessment_step) = saved


def run_unit(run: Run, config: str, entry: dict, settings: dict, steps=None) -> dict:
    """Put one claim through one configuration and save what happened."""
    claim_id = entry["claim_id"]
    intake_step, assessment_step = steps or steps_for(config, run.stand_in)
    console = run.path(config, "console", f"{claim_id}.txt")
    console.parent.mkdir(parents=True, exist_ok=True)

    started_at, started = _now(), time.time()
    error = None
    with flow_redirected(run, config, intake_step, assessment_step):
        flow = claim_flow.ClaimFlow()
        with console.open("w") as out, contextlib.redirect_stdout(out):
            try:
                with observability.claim_span(claim_id, evaluation=run.dir.name,
                                              config=config):
                    flow.kickoff(inputs={"claim_id": claim_id, "interactive": False,
                                         "use_saved_intake": True, "verbose": False})
            except Exception as exc:                                  # noqa: BLE001
                error = exc
    seconds = round(time.time() - started, 2)
    log = _log_entries(run.path(config, "logs", f"{claim_id}.jsonl"))
    crews = {e["crew"]: e for e in log if e.get("event") == "crew_done"}
    failures = [e for e in log if e.get("event") == "llm_call_failed"]

    usage = {name: {k: crews[name].get(k, 0) for k in monitoring.USAGE_FIELDS}
             for name in crews if crews[name].get("total_tokens") is not None}
    if "intake" in usage:                    # the intake ran now; it is shared from here
        run.write(usage["intake"], "intake", f"{claim_id}.usage.json")
    run.spend(settings["model"], sum(u.get("total_tokens", 0) for u in usage.values()))

    if error is not None or flow.state.status not in claim_flow.FINAL_STATUSES:
        kinds = sorted({e.get("kind") for e in failures})
        reason = (str(error).splitlines()[0][:300] if error else
                  f"the Flow stopped without a decision (status {flow.state.status or 'none'})")
        previous = run.read(config, f"{claim_id}.error.json", default={})
        run.write({"claim_id": claim_id, "config": config, "at": started_at,
                   "attempts": previous.get("attempts", 0) + 1, "error": reason,
                   "llm_errors": kinds, "seconds": seconds,
                   "tokens_reported": usage}, config, f"{claim_id}.error.json")
        if "daily_token_limit" in kinds or "tokens per day" in reason.lower():
            raise StopRun(f"the provider's daily token limit was reached on {claim_id} "
                          f"(config {config}). Run the same command again tomorrow.")
        return {"claim_id": claim_id, "config": config, "error": reason}

    state = flow.state.model_dump()
    decision, settlement = state.get("decision") or {}, state.get("settlement") or {}
    finished = next((e for e in log if e.get("event") == "run_finished"), {})
    record = {
        "claim_id": claim_id, "config": config,
        "fault_type": entry["fault_type"], "split": entry["split"],
        "model": settings["model"], "prompt_version": settings["prompt_version"],
        "intake_prompt_version": settings["intake_prompt_version"],
        "decision_code": settings["decision_code"],
        "tool_configuration": settings["tool_configuration"],
        "stand_in": settings["stand_in"], "code_version": git_version(),
        "started_at": started_at, "seconds": seconds,
        "status": state["status"], "route": state["route"],
        "coverage_status": decision.get("coverage_status"),
        "fraud_score": decision.get("fraud_score"),
        "fraud_flags": decision.get("fraud_flags", []),
        "line_decisions": [{k: line.get(k) for k in ("description", "amount", "admitted", "reason")}
                           for line in decision.get("line_decisions", [])],
        "proposed_payout": settlement.get("payout") if settlement else None,
        "coverage_doubts": state.get("coverage_doubts", []),
        "payout_doubts": state.get("payout_doubts", []),
        "guardrail_flags": state.get("guardrail_flags", []),
        "intake_reused": "intake" not in usage,
        "tokens": {"assessment": usage.get("assessment")},
        "llm_errors": finished.get("llm_errors", []),
        "retried_llm_failures": finished.get("retried_llm_failures", 0),
        "trace_id": finished.get("trace_id"),
    }
    run.write(record, config, f"{claim_id}.json")
    run.path(config, f"{claim_id}.error.json").unlink(missing_ok=True)
    return record


def evaluate(run: Run, configs=AVAILABLE, claims=None, daily_tokens=DEFAULT_DAILY_TOKENS,
             max_units=None, allow_changes=False, steps=None, say=print) -> dict:
    """Work through every claim for every configuration, a claim at a time
    (A then B for the same claim), so a run stopped part-way still compares
    the two on the same claims. Returns what happened this time."""
    settings = run.check_settings(configs, allow_changes)
    entries = select_claims(claims)
    ran, failed, stopped, in_a_row = [], [], None, 0
    try:
        for entry in entries:
            for config in configs:
                if run.done(config, entry["claim_id"]):
                    continue
                if max_units is not None and len(ran) + len(failed) >= max_units:
                    raise StopRun(f"stopped after {max_units} unit(s), as asked")
                model = settings[config]["model"]
                if daily_tokens:
                    need = run.estimate(config, entry["claim_id"])
                    spent = run.spent_today(model)
                    if spent + need > daily_tokens:
                        raise StopRun(
                            f"today's budget: {spent:,} of {daily_tokens:,} tokens spent on "
                            f"{model}, and the next claim needs about {need:,}. "
                            f"Run the same command again tomorrow.")
                result = run_unit(run, config, entry, settings[config],
                                  steps=(steps or {}).get(config))
                if "error" in result:
                    failed.append(result)
                    in_a_row += 1
                    say(f"  {config} {entry['claim_id']}  FAILED  {result['error'][:90]}")
                    if in_a_row >= FAILURES_IN_A_ROW:
                        raise StopRun(f"{in_a_row} failures in a row; the last: "
                                      f"{result['error'][:200]}")
                else:
                    in_a_row = 0
                    ran.append(result)
                    total = (result["tokens"]["assessment"] or {}).get("total_tokens", 0)
                    say(f"  {config} {entry['claim_id']}  {result['route']:<14} "
                        f"{result['seconds']:>7.1f}s  {total:>7,} tokens")
    except StopRun as why:
        stopped = str(why)
        say(f"\n  {stopped}")
    remaining = sum(not run.done(c, e["claim_id"]) for e in entries for c in configs)
    return {"ran": ran, "failed": failed, "stopped": stopped, "remaining": remaining}


# ---------------------------------------------------------------- scoring
def outcome(record: dict) -> str:
    """approve, reject or escalate. A claim sent to a person, or one waiting
    for evidence, is an escalation: neither a correct approval nor a correct
    rejection, and never counted as either."""
    return {"auto_approve": "approve", "reject": "reject"}.get(record.get("route"), "escalate")


def expected_outcome(truth: dict) -> str:
    """What a correct adjuster does: refuse a claim that is not covered or has
    nothing payable (a duplicate), and pay the rest."""
    if not truth["is_covered"] or truth["expected_payout"] <= 0:
        return "reject"
    return "approve"


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def line_counts(record: dict, truth: dict) -> dict:
    """Denied lines are the positive class: the job is to find the lines that
    should not be paid. A line the decision leaves out is not paid, so it
    counts as denied. Unmatched decision lines are counted, not scored."""
    predicted = {}
    for line in record.get("line_decisions") or []:
        predicted.setdefault(_norm(line["description"]), []).append(bool(line.get("admitted")))
    tp = fp = fn = tn = 0
    for line in truth["lines"]:
        said = predicted.get(_norm(line["description"]))
        admitted = said.pop(0) if said else False
        should_deny = not line["admitted"]
        if should_deny and not admitted:
            tp += 1
        elif should_deny:
            fn += 1
        elif not admitted:
            fp += 1
        else:
            tn += 1
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "unmatched": sum(len(v) for v in predicted.values())}


def _ratio(top, bottom):
    return round(top / bottom, 4) if bottom else None


def _f1(precision, recall):
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 4)


def score(records: list, truths: dict) -> dict:
    """The metrics for one configuration over the given records."""
    n = len(records)
    out = {"claims": n}
    if not n:
        return out

    outcomes = {"correct_approve": 0, "correct_reject": 0, "wrong_approve": 0,
                "wrong_reject": 0, "escalated": 0}
    coverage = {"correct": 0, "wrong": 0, "uncertain": 0, "not_assessed": 0}
    fraud = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    lines = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "unmatched": 0}
    errors, escalated_by_fault = [], {}

    for record in records:
        truth = truths[record["claim_id"]]
        got, want = outcome(record), expected_outcome(truth)
        fault = truth["fault_type"]
        bucket = escalated_by_fault.setdefault(fault, [0, 0])
        bucket[1] += 1
        if got == "escalate":
            outcomes["escalated"] += 1
            bucket[0] += 1
        else:
            outcomes[f"{'correct' if got == want else 'wrong'}_{got}"] += 1

        status = record.get("coverage_status")
        if status is None:
            coverage["not_assessed"] += 1
        elif status == "uncertain":
            coverage["uncertain"] += 1
        else:
            coverage["correct" if (status == "covered") == truth["is_covered"] else "wrong"] += 1

        if record.get("fraud_score") is not None:
            said = record["fraud_score"] >= claim_flow.FRAUD_THRESHOLD
            key = ("t" if said == truth["is_fraudulent"] else "f") + ("p" if said else "n")
            fraud[key] += 1

        # Line decisions mean nothing on a claim that is out of cover as a
        # whole: every line is refused with it, rightly, though each line
        # on its own would have been payable.
        if truth["is_covered"] and record.get("line_decisions") is not None:
            for k, v in line_counts(record, truth).items():
                lines[k] += v

        if record.get("proposed_payout") is not None:
            errors.append(abs(record["proposed_payout"] - truth["expected_payout"]))

    decided = n - outcomes["escalated"]
    judged = coverage["correct"] + coverage["wrong"]
    precision = _ratio(lines["tp"], lines["tp"] + lines["fp"])
    recall = _ratio(lines["tp"], lines["tp"] + lines["fn"])
    out.update({
        "outcomes": outcomes,
        "correct_decision_rate": _ratio(outcomes["correct_approve"] + outcomes["correct_reject"], n),
        "accuracy_when_decided": _ratio(outcomes["correct_approve"] + outcomes["correct_reject"],
                                        decided),
        "escalation_rate": _ratio(outcomes["escalated"], n),
        "escalation_by_fault_type": {k: {"escalated": v[0], "claims": v[1]}
                                     for k, v in sorted(escalated_by_fault.items())},
        "coverage": coverage,
        "coverage_accuracy": _ratio(coverage["correct"], n),
        "coverage_accuracy_when_judged": _ratio(coverage["correct"], judged),
        "fraud": fraud,
        "fraud_precision": _ratio(fraud["tp"], fraud["tp"] + fraud["fp"]),
        "fraud_recall": _ratio(fraud["tp"], fraud["tp"] + fraud["fn"]),
        "line_items": lines,
        "line_precision": precision,
        "line_recall": recall,
        "line_f1": _f1(precision, recall),
        "payout_mae": round(statistics.mean(errors), 2) if errors else None,
        "payout_median_error": round(statistics.median(errors), 2) if errors else None,
        "payout_max_error": round(max(errors), 2) if errors else None,
        "payout_within_1": sum(e <= 1.0 for e in errors),
        "payout_scored": len(errors),
    })
    seconds = [r["seconds"] for r in records]
    assessment = [r["tokens"]["assessment"] for r in records if r["tokens"].get("assessment")]
    out.update({
        "seconds_mean": round(statistics.mean(seconds), 2),
        "seconds_median": round(statistics.median(seconds), 2),
        "seconds_total": round(sum(seconds), 2),
        "tokens_counted": len(assessment),
    })
    for field in ("total_tokens", "prompt_tokens", "completion_tokens", "reasoning_tokens",
                  "cached_prompt_tokens", "successful_requests"):
        values = [u.get(field, 0) for u in assessment]
        out[f"assessment_{field}_mean"] = round(statistics.mean(values)) if values else None
    return out


def intake_usage(run: Run, claim_ids) -> dict:
    found = [run.read("intake", f"{c}.usage.json") for c in claim_ids]
    found = [u for u in found if u]
    return {"claims": len(found),
            "total_tokens_mean": round(statistics.mean(u["total_tokens"] for u in found))
            if found else None}


def report(run: Run, configs=CONFIGS) -> dict:
    """Score what is saved. Compared on the claims every available
    configuration has finished, so A and B are always read on the same set."""
    by_config = {c: {r["claim_id"]: r for r in run.records(c)} for c in configs
                 if c not in NOT_AVAILABLE}
    started = [v for v in by_config.values() if v]       # a config never run is not compared
    paired = sorted(set.intersection(*(set(v) for v in started))) if started else []
    truths = {c: truth_for(c) for c in paired}
    intake = intake_usage(run, paired)
    summary = {
        "run": run.dir.name, "generated": _now(), "stand_in": run.stand_in,
        "settings": run.read("run.json", default={}).get("configs", {}),
        "paired_claims": paired,
        "unpaired": {c: sorted(set(v) - set(paired)) for c, v in by_config.items()},
        "intake": intake,
        "configs": {},
        "notes": [
            "Escalations are their own category: neither a correct approval nor a "
            "correct rejection.",
            "Fraud claims are routed to a person by design (fraud score >= "
            f"{claim_flow.FRAUD_THRESHOLD}), so their escalation is expected; see "
            "escalation_by_fault_type.",
            "Line-item scores count denied lines as the positive class, and leave out "
            "claims that are not covered at all.",
            "Payouts are the settlement calculator's, from each configuration's line "
            "decisions; the calculator is shared.",
            "Tokens are as the provider reported them. No money cost is estimated.",
            "Development claims only: the prompts were tuned on these, which favours B.",
        ],
    }
    for config in configs:
        if config in NOT_AVAILABLE:
            summary["configs"][config] = {"available": False, "why": NOT_AVAILABLE[config]}
            continue
        scored = score([by_config[config][c] for c in paired if c in by_config[config]],
                       truths)
        if intake["total_tokens_mean"] is not None and scored.get("assessment_total_tokens_mean") is not None:
            scored["end_to_end_tokens_mean"] = (scored["assessment_total_tokens_mean"]
                                                + intake["total_tokens_mean"])
        summary["configs"][config] = {"available": True, **scored}
    return summary


# ---------------------------------------------------------------- output
TABLE = [
    ("claims compared", "claims", "{}"),
    ("correct decisions", "correct_decision_rate", "pct"),
    ("  accuracy when decided", "accuracy_when_decided", "pct"),
    ("escalation rate", "escalation_rate", "pct"),
    ("coverage accuracy", "coverage_accuracy", "pct"),
    ("  when judged (no 'uncertain')", "coverage_accuracy_when_judged", "pct"),
    ("fraud precision", "fraud_precision", "pct"),
    ("fraud recall", "fraud_recall", "pct"),
    ("line-item F1 (denied lines)", "line_f1", "{:.3f}"),
    ("payout error, mean (Rs)", "payout_mae", "{:,.2f}"),
    ("payout error, max (Rs)", "payout_max_error", "{:,.2f}"),
    ("payouts within Rs 1", "payout_within_1", "{}"),
    ("seconds per claim, mean", "seconds_mean", "{:.1f}"),
    ("seconds per claim, median", "seconds_median", "{:.1f}"),
    ("assessment tokens, mean", "assessment_total_tokens_mean", "{:,}"),
    ("  prompt", "assessment_prompt_tokens_mean", "{:,}"),
    ("  completion", "assessment_completion_tokens_mean", "{:,}"),
    ("  reasoning", "assessment_reasoning_tokens_mean", "{:,}"),
    ("  LLM requests", "assessment_successful_requests_mean", "{:,}"),
    ("end-to-end tokens, mean", "end_to_end_tokens_mean", "{:,}"),
]


def _cell(value, fmt) -> str:
    if value is None:
        return "n/a"
    if fmt == "pct":
        return f"{value * 100:.1f}%"
    return fmt.format(value)


def table(summary: dict) -> str:
    configs = list(summary["configs"])
    rows = [("", *configs)]
    for label, key, fmt in TABLE:
        rows.append((label, *(_cell(summary["configs"][c].get(key), fmt)
                              if summary["configs"][c]["available"] else "not avail."
                              for c in configs)))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(widths[0]) if i == 0 else cell.rjust(widths[i])
                       for i, cell in enumerate(row)) for row in rows]
    lines.insert(1, "-" * len(lines[0]))
    head = [f"run {summary['run']}: {len(summary['paired_claims'])} claim(s) finished "
            f"by every available configuration"]
    if summary["stand_in"]:
        head.insert(0, "STAND-IN REHEARSAL: these numbers measure the harness, not ClaimSight")
    intake = summary["intake"]
    tail = [f"intake (shared by A and B): {intake['total_tokens_mean'] or 'n/a'} tokens per "
            f"claim, mean over {intake['claims']}"]
    for config, info in summary["configs"].items():
        if not info["available"]:
            tail.append(f"{config}: not available - {info['why']}")
    for config, unpaired in summary["unpaired"].items():
        if unpaired:
            tail.append(f"{config} also finished {len(unpaired)} claim(s) not yet compared: "
                        f"{', '.join(unpaired)}")
    return "\n".join(head + [""] + lines + [""] + tail)


CSV_FIELDS = ["config", "claim_id", "fault_type", "status", "route", "outcome",
              "expected_outcome", "coverage_status", "is_covered", "fraud_score",
              "is_fraudulent", "proposed_payout", "expected_payout", "payout_error",
              "lines_tp", "lines_fp", "lines_fn", "seconds", "assessment_total_tokens",
              "assessment_prompt_tokens", "assessment_completion_tokens",
              "assessment_reasoning_tokens", "intake_total_tokens", "model",
              "prompt_version", "intake_prompt_version", "decision_code",
              "tool_configuration", "stand_in", "code_version", "started_at", "trace_id"]


def write_outputs(run: Run, summary: dict) -> dict:
    rows = []
    for config in summary["configs"]:
        if config in NOT_AVAILABLE:
            continue
        for record in run.records(config):
            truth = truth_for(record["claim_id"])
            usage = record["tokens"].get("assessment") or {}
            intake = run.read("intake", f"{record['claim_id']}.usage.json") or {}
            counts = (line_counts(record, truth) if truth["is_covered"] else {})
            payout = record.get("proposed_payout")
            rows.append({
                **{k: record.get(k) for k in CSV_FIELDS if k in record},
                "outcome": outcome(record), "expected_outcome": expected_outcome(truth),
                "is_covered": truth["is_covered"], "is_fraudulent": truth["is_fraudulent"],
                "expected_payout": truth["expected_payout"],
                "payout_error": (round(abs(payout - truth["expected_payout"]), 2)
                                 if payout is not None else None),
                "lines_tp": counts.get("tp"), "lines_fp": counts.get("fp"),
                "lines_fn": counts.get("fn"),
                **{f"assessment_{k}": usage.get(k) for k in
                   ("total_tokens", "prompt_tokens", "completion_tokens", "reasoning_tokens")},
                "intake_total_tokens": intake.get("total_tokens"),
            })
    paths = {"json": run.path("results.json"), "csv": run.path("results.csv"),
             "summary_csv": run.path("summary.csv")}
    run.write({**summary, "claims": rows}, "results.json")
    with paths["csv"].open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    with paths["summary_csv"].open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", *summary["configs"]])
        for label, key, _ in TABLE:
            writer.writerow([key, *(summary["configs"][c].get(key, "not available")
                                    for c in summary["configs"])])
    return paths


# ---------------------------------------------------------------- command line
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", default="phase2-dev", help="name of the run folder")
    parser.add_argument("--configs", default="A,B", help="which to run, e.g. B or A,B")
    parser.add_argument("--claims", default="", help="comma-separated development claim ids")
    parser.add_argument("--daily-tokens", type=int, default=DEFAULT_DAILY_TOKENS,
                        help="stop before passing this many tokens today (0: no limit)")
    parser.add_argument("--max-units", type=int, default=None,
                        help="stop after this many claim-configuration runs")
    parser.add_argument("--report", action="store_true", help="score what is saved; no model call")
    parser.add_argument("--stand-in", action="store_true",
                        help="stand-in crews: costs nothing, measures nothing")
    parser.add_argument("--allow-changes", action="store_true",
                        help="resume although the settings have changed")
    args = parser.parse_args(argv)

    configs = [c.strip().upper() for c in args.configs.split(",") if c.strip()]
    for config in configs:
        if config in NOT_AVAILABLE:
            print(f"config {config} is not available: {NOT_AVAILABLE[config]}")
        elif config not in CONFIGS:
            raise SystemExit(f"unknown config {config}; choose from {', '.join(CONFIGS)}")
    configs = [c for c in configs if c not in NOT_AVAILABLE]

    run = Run(args.run, stand_in=args.stand_in)
    saved = run.read("run.json")
    if saved and saved.get("stand_in") != args.stand_in:
        raise SystemExit(f"run '{args.run}' is a {'stand-in' if saved['stand_in'] else 'real'} "
                         f"run; it cannot be continued "
                         f"{'without' if saved['stand_in'] else 'with'} --stand-in")

    if not args.report:
        claims = [c.strip() for c in args.claims.split(",") if c.strip()] or None
        print(f"evaluating {len(select_claims(claims))} development claim(s), "
              f"configs {','.join(configs)}, into {run.dir}")
        session = evaluate(run, configs=configs, claims=claims,
                           daily_tokens=args.daily_tokens, max_units=args.max_units,
                           allow_changes=args.allow_changes)
        print(f"\n  this session: {len(session['ran'])} finished, {len(session['failed'])} "
              f"failed, {session['remaining']} still to run")
        observability.flush()

    summary = report(run)
    paths = write_outputs(run, summary)
    print("\n" + table(summary))
    print(f"\nwritten: {paths['json']}\n         {paths['csv']}\n         {paths['summary_csv']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
