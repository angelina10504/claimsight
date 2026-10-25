"""
Assessment crew.

Three agents:
  Policy Analyst    - decides coverage from the policy wording (Knowledge / RAG)
                      and checks the cover dates with a tool
  Fraud Analyst     - checks prices and earlier claims with tools, then weighs
                      what it finds (ReAct: reason, call a tool, observe, repeat)
  Assessment Lead   - combines both reports into a typed AssessmentDecision

The two analysts run in PARALLEL (async_execution=True): neither needs the
other's result, so the crew starts both and the Lead waits for the pair.

Design note: the analysts use tools and write plain-text reports; only the
Lead, who has no tools, returns structured output. Asking a tool-using agent
for structured output makes some models call a non-existent 'json' tool (the
failure seen in the Intake crew), so the two jobs are kept apart.

No agent computes money. The Lead marks each invoice line admitted or not, and
the Flow passes those lines to the settlement calculator.

Tools come from the ClaimSight MCP server (src/mcp_server.py), each agent seeing
only the tools it needs. Set USE_MCP=false in .env to fall back to calling the
same functions directly, e.g. if the server will not start during a demo.
"""

import os
import sys
from pathlib import Path

from crewai import Agent, Crew, Process, Task
from crewai.mcp import MCPServerStdio, create_static_tool_filter
from crewai.knowledge.source.text_file_knowledge_source import TextFileKnowledgeSource

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from config import get_llm                                   # noqa: E402
from monitoring import crew_callbacks                        # noqa: E402
import guardrails                                            # noqa: E402
from models import AssessmentDecision                        # noqa: E402
from tools.assessment_tools import (                         # noqa: E402
    PolicyPeriodTool,
    PriceCheckTool,
    PriorClaimsTool,
)

# Local embeddings, as in test_policy_knowledge.py, so the index is reused
EMBEDDER = {"provider": "ollama", "config": {"model_name": "nomic-embed-text"}}

USE_MCP = os.getenv("USE_MCP", "true").lower() != "false"


def tool_access(*names: str) -> dict:
    """Agent kwargs giving access to exactly these tools: over MCP by default,
    or as direct CrewAI tools when USE_MCP=false.

    The server is launched through ./claimsight_tools on purpose: CrewAI
    prefixes each MCP tool name with the launch command, so the tools are named
    "claimsight_tools_<tool>". Launched as "python ..." they started with
    "python_", and gpt-oss confused them with its built-in "python" tool."""
    if USE_MCP:
        server = MCPServerStdio(
            command="./claimsight_tools",
            args=[],
            tool_filter=create_static_tool_filter(allowed_tool_names=list(names)),
        )
        return {"mcps": [server]}
    direct = {"check_policy_period": PolicyPeriodTool, "check_part_prices": PriceCheckTool,
              "find_prior_claims": PriorClaimsTool}
    return {"tools": [direct[n]() for n in names]}


def build_assessment_crew(verbose: bool = True, monitor=None) -> Crew:
    guardrails.install()       # tool-result checks and PII redaction on every LLM call
    llm = get_llm("text")

    policy_wording = TextFileKnowledgeSource(
        file_paths=["policy_wording.txt"],
        chunk_size=1200,
        chunk_overlap=200,
    )

    # Same role string as the Step 1 test, so CrewAI reuses that embedded index
    policy_analyst = Agent(
        role="Motor Insurance Policy Analyst",
        goal="Decide whether a claim is covered, citing the policy clause for every finding.",
        backstory=(
            "You interpret the Private Car Package Policy for a claims team. You never answer "
            "from general knowledge: if the wording does not say it, you say it is not stated. "
            "Section I lists the insured perils and the depreciation on parts. The exclusions "
            "start with 'The Company shall not be liable to make any payment in respect of'."
        ),
        llm=llm,
        **tool_access("check_policy_period"),
        knowledge_sources=[policy_wording],
        embedder=EMBEDDER,
        verbose=verbose,
        allow_delegation=False,
    )

    fraud_analyst = Agent(
        role="Motor Claims Fraud Analyst",
        goal="Find evidence of inflated, repeated or inconsistent claims, using the tools for every fact.",
        backstory=(
            "You have investigated motor claims for years. You know most claims are honest, so "
            "you flag only what the evidence shows: a price far above market, the same damage "
            "claimed twice, or a story that does not fit the parts billed. You never guess a price "
            "or a claim history; you look it up."
        ),
        llm=llm,
        **tool_access("check_part_prices", "find_prior_claims"),
        verbose=verbose,
        allow_delegation=False,
    )

    assessment_lead = Agent(
        role="Claims Assessment Lead",
        goal="Turn the coverage and fraud reports into one consistent, line-by-line decision.",
        backstory=(
            "You sign off assessments before payment. You apply the team's rules exactly and "
            "never invent findings the analysts did not report. You do not calculate payouts; "
            "the settlement system does that from your line decisions."
        ),
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )

    coverage_task = Task(
        description=(
            "Assess coverage for claim {claim_id}.\n\n"
            "Incident date: {incident_date}\n"
            "What happened: {narrative}\n"
            "Policy schedule:\n{policy}\n"
            "Invoice lines:\n{line_items}\n\n"
            "1. Use the check_policy_period tool with policy {policy_no} and incident date {incident_date}.\n"
            "2. Using the policy wording, decide whether the cause described is an insured peril "
            "under Section I. Collisions, impacts, being hit by another vehicle, reversing into "
            "an object and stone damage are all 'accidental external means' (Section I, item 6).\n"
            "3. Check every invoice line against the exclusions: wear and tear, mechanical or "
            "electrical breakdown, consequential loss, and tyres unless the vehicle was damaged "
            "at the same time. Replacement of body parts damaged in the accident is not wear and tear.\n\n"
            "Write a COVERAGE REPORT with three headings:\n"
            "PERIOD: inside or outside, as the tool reported\n"
            "PERIL: covered or not covered, with the clause\n"
            "EXCLUDED LINES: each excluded line exactly as printed, with the clause, or 'none'"
        ),
        expected_output="A coverage report with PERIOD, PERIL and EXCLUDED LINES, each citing its source.",
        agent=policy_analyst,
        async_execution=True,
        guardrail=guardrails.coverage_report_guardrail,
        guardrail_max_retries=2,
    )

    fraud_task = Task(
        description=(
            "Investigate claim {claim_id} for fraud.\n\n"
            "Policy: {policy_no}\n"
            "Incident date: {incident_date}\n"
            "What happened: {narrative}\n"
            "Parts claimed: {damaged_parts}\n"
            "Invoice lines:\n{line_items}\n\n"
            "1. Use the check_part_prices tool with every invoice line (description and amount).\n"
            "2. Use the find_prior_claims tool with policy {policy_no}, claim {claim_id}, incident date "
            "{incident_date} and the parts claimed.\n"
            "3. Consider whether the parts billed fit what the claimant says happened.\n\n"
            "Write a FRAUD REPORT with four headings:\n"
            "PRICES: each line the price check flagged, quoted from the tool, or 'none flagged'\n"
            "PRIOR CLAIMS: what find_prior_claims returned\n"
            "CONSISTENCY: whether the parts fit the story, in one or two sentences\n"
            "RISK: low, medium or high, with the reason\n"
            "Report only what the tools and the claim show."
        ),
        expected_output="A fraud report with PRICES, PRIOR CLAIMS, CONSISTENCY and RISK.",
        agent=fraud_analyst,
        async_execution=True,
        guardrail=guardrails.fraud_report_guardrail,
        guardrail_max_retries=2,
    )

    decision_task = Task(
        description=(
            "Combine the coverage report and the fraud report for claim {claim_id} into the "
            "final assessment.\n\n"
            "Invoice lines:\n{line_items}\n\n"
            "Apply these rules in order:\n"
            "1. If the incident is outside the policy period, or the coverage report names a "
            "specific reason the cause is not an insured peril: is_covered = false, admit no "
            "lines, route = reject. If the report only says it could not find a clause, the "
            "claim stays covered; note the doubt in fraud_flags and use route human_review.\n"
            "2. If the fraud report says the same parts were already claimed earlier: admit NO "
            "lines, each with the reason 'same damage already claimed', fraud_score = 0.9.\n"
            "3. A line the price check flagged as overpriced is not admitted (reason: priced far "
            "above market rate); fraud_score at least 0.7.\n"
            "4. A line the coverage report lists as excluded is not admitted, with the clause as "
            "the reason. An exclusion on its own is not fraud.\n"
            "5. Every other line is admitted, including painting and labour.\n"
            "6. With nothing suspicious, fraud_score is between 0.0 and 0.2.\n"
            "7. route: reject if not covered; human_review if fraud_score is 0.5 or more; "
            "otherwise auto_approve.\n\n"
            "line_decisions must contain EVERY invoice line, with its description, amount and "
            "material exactly as given above. Do not calculate any payout."
        ),
        expected_output="An AssessmentDecision covering every invoice line.",
        agent=assessment_lead,
        context=[coverage_task, fraud_task],
        output_pydantic=AssessmentDecision,
        guardrail=guardrails.decision_guardrail,     # every line, exact amounts, no PII
        guardrail_max_retries=2,
    )

    return Crew(
        agents=[policy_analyst, fraud_analyst, assessment_lead],
        tasks=[coverage_task, fraud_task, decision_task],
        process=Process.sequential,
        verbose=verbose,
        max_rpm=10,          # stay inside the free-tier request limit
        **crew_callbacks(monitor),     # step and task callbacks -> data/logs/<claim>.jsonl
    )
