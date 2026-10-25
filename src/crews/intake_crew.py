"""
Intake crew.

Two agents, run sequentially:
  Document Reader   - turns raw invoice text into structured line items
  Claim Assembler   - merges that with the submission and policy, judges
                      completeness

Design note: the invoice, submission and policy are fetched in plain Python and
handed to the agents as inputs. Reading a known file is not a decision, so it
is not a tool call. Keeping these agents tool-free also avoids a failure mode
where the model emits its final structured answer as a call to a tool that
does not exist. Tools are reserved for agents that genuinely choose what to
look up (the Fraud Analyst, later).

The Damage Analyst (vision over the claimant's photos) joins this crew once the
photo labels exist; until then damaged_parts is derived from the invoice.
"""

import sys
from pathlib import Path

from crewai import Agent, Crew, Process, Task

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from config import get_llm, max_rpm                            # noqa: E402
from monitoring import crew_callbacks, usage_fields            # noqa: E402
import guardrails                                              # noqa: E402
from models import ClaimIntake, ExtractedInvoice               # noqa: E402
from tools.claim_tools import (                                # noqa: E402
    ReadClaimSubmission,
    ReadInvoicePDF,
    ReadPolicySchedule,
)


PII_FIELDS = ("insured_name", "phone", "address")


def fetch_inputs(claim_id: str) -> dict:
    """Deterministic data gathering. No LLM involved.

    Everything an agent is about to read is cleaned here, because this is the
    last point at which it is still only on this machine:

      the narrative   screened for prompt injection and abuse
      the policy      name, phone and address dropped before it is passed on
      the invoice     the policyholder's name and any address, phone, GSTIN or
                      labelled personal field removed from the document text

    The invoice matters most: it is a document nobody wrote for a machine, and
    it carries the customer's name in plain sight. The policy schedule gives us
    that name, so it can be removed exactly rather than guessed at. If anything
    personal survives, safe_for_cloud raises and the claim stops here."""
    import json

    submission_text = ReadClaimSubmission()._run(claim_id)
    if submission_text.startswith("ERROR"):
        raise FileNotFoundError(submission_text)
    submission = json.loads(submission_text)
    submission["narrative"], _ = guardrails.safe_narrative(submission.get("narrative", ""))

    policy_text = ReadPolicySchedule()._run(submission["policy_no"])
    policy_on_file = None
    if not policy_text.startswith("ERROR"):
        policy_on_file = json.loads(policy_text)
        policy_text = json.dumps({k: v for k, v in policy_on_file.items()
                                  if k not in PII_FIELDS}, indent=2)

    invoice_text = ReadInvoicePDF()._run(claim_id)
    if not invoice_text.startswith("ERROR"):
        invoice_text = guardrails.safe_for_cloud(
            invoice_text, policy_on_file, what=f"the invoice for {claim_id}")

    return {
        "claim_id": claim_id,
        "invoice_text": invoice_text,
        "submission": guardrails.safe_for_cloud(
            json.dumps(submission, indent=2), policy_on_file,
            what=f"the claim submission for {claim_id}"),
        "policy": policy_text,
    }


def build_intake_crew(verbose: bool = True, monitor=None) -> Crew:
    guardrails.install()       # tool-result checks and PII redaction on every LLM call
    llm = get_llm("text")

    document_reader = Agent(
        role="Motor Claims Document Reader",
        goal=(
            "Extract every line item from a garage repair invoice exactly as "
            "billed, with no amount invented, omitted, or rounded."
        ),
        backstory=(
            "You have processed thousands of garage bills for an Indian motor "
            "insurer. You read what is on the page and nothing more. Where a "
            "field is absent from the invoice you report it as absent rather "
            "than guessing, because a guessed amount becomes a wrong payout."
        ),
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )

    claim_assembler = Agent(
        role="Claims Intake Officer",
        goal=(
            "Assemble a complete, structured claim record and decide honestly "
            "whether there is enough evidence to assess it."
        ),
        backstory=(
            "You are the first person a claim meets. Your job is not to judge "
            "the claim but to make sure the file is complete: the policy "
            "exists, the vehicle on the invoice matches the policy, the dates "
            "are present. You would rather send a claim back for one missing "
            "document than let an assessor work from a half-empty file."
        ),
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )

    extract_invoice = Task(
        description=(
            "Below is the text of the repair invoice for claim {claim_id}.\n\n"
            "--- INVOICE START ---\n{invoice_text}\n--- INVOICE END ---\n\n"
            "Extract:\n"
            "- invoice number, invoice date (as YYYY-MM-DD), garage name\n"
            "- the vehicle registration number\n"
            "- every line item with its exact description and rupee amount\n"
            "- the subtotal, the GST amount, and the total\n\n"
            "For each line item also set `material` to one of: plastic, metal, "
            "glass, fibreglass, tyre, battery, paint, labour. Bumpers, "
            "headlights, taillights, mirrors and grilles are plastic. Doors, "
            "bonnets, fenders and panels are metal. Windshields and window "
            "glass are glass. Painting is paint. Labour and service charges "
            "are labour. If you cannot tell, leave material null.\n\n"
            "Report amounts as plain numbers: 9000, not 'Rs 9,000'. Do not "
            "add lines that are not on the invoice. Do not drop any line."
        ),
        expected_output=(
            "An ExtractedInvoice with every billed line present and the "
            "amounts matching the invoice exactly."
        ),
        agent=document_reader,
        output_pydantic=ExtractedInvoice,
    )

    assemble_claim = Task(
        description=(
            "Build the intake record for claim {claim_id}.\n\n"
            "What the claimant submitted:\n{submission}\n\n"
            "The policy schedule on file:\n{policy}\n\n"
            "The extracted invoice is provided by the previous task.\n\n"
            "Derive `damaged_parts` from the invoice line items: list the "
            "physical parts billed for replacement, lowercase and without the "
            "word 'replacement' (for example 'front bumper', 'headlight'). "
            "Exclude painting and labour lines - they are not parts.\n\n"
            "Set `is_complete` to false and name the gaps in `missing_fields` "
            "if any of these hold:\n"
            "- the policy schedule is missing or shows an ERROR\n"
            "- the invoice has no line items or no total\n"
            "- the incident date is missing\n"
            "- the registration on the invoice does not match the policy\n\n"
            "Do not assess coverage, fraud, or payout. That is not your job "
            "and anything you say about it will be discarded. Keep `notes` to "
            "two lines at most."
        ),
        expected_output=(
            "A ClaimIntake for {claim_id} with the invoice attached, "
            "damaged_parts derived, and an honest completeness verdict."
        ),
        agent=claim_assembler,
        context=[extract_invoice],
        output_pydantic=ClaimIntake,
    )

    return Crew(
        agents=[document_reader, claim_assembler],
        tasks=[extract_invoice, assemble_claim],
        process=Process.sequential,
        verbose=verbose,
        cache=True,
        max_rpm=max_rpm(),
        **crew_callbacks(monitor),     # step and task callbacks -> data/logs/<claim>.jsonl
    )


def run_intake(claim_id: str, verbose: bool = True, monitor=None) -> ClaimIntake:
    inputs = fetch_inputs(claim_id)
    crew = build_intake_crew(verbose=verbose, monitor=monitor)
    result = crew.kickoff(inputs=inputs)
    if monitor is not None:      # the Flow puts this in its intake crew_done entry
        monitor.crew_usage["intake"] = usage_fields(getattr(result, "token_usage", None))
    return result.pydantic
