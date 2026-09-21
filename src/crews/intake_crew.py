"""
Intake crew.

Two agents, run sequentially:
  Document Reader   - pulls structured line items out of the repair invoice
  Claim Assembler   - merges that with the submission and judges completeness

The crew's output is a validated ClaimIntake. Everything downstream branches on
its fields, so this is the point where free text becomes structured data.

The Damage Analyst (vision over the claimant's photos) joins this crew once the
photo labels exist; until then damaged_parts is inferred from the invoice.
"""

import sys
from pathlib import Path

from crewai import Agent, Crew, Process, Task

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from config import get_llm                                     # noqa: E402
from models import ClaimIntake, ExtractedInvoice               # noqa: E402
from tools.claim_tools import (                                # noqa: E402
    ReadClaimSubmission,
    ReadInvoicePDF,
    ReadPolicySchedule,
)


def build_intake_crew(verbose: bool = True) -> Crew:
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
        tools=[ReadInvoicePDF()],
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
            "You are the first human a claim meets. Your job is not to judge "
            "the claim but to make sure the file is complete: the policy "
            "exists, the vehicle on the invoice matches the policy, the dates "
            "are present. You would rather send a claim back for one missing "
            "document than let an assessor work from a half-empty file."
        ),
        tools=[ReadClaimSubmission(), ReadPolicySchedule()],
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
    )

    extract_invoice = Task(
        description=(
            "Read the repair invoice for claim {claim_id} using the "
            "read_invoice_pdf tool.\n\n"
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
            "1. Use read_claim_submission to get the policy number, incident "
            "date, narrative and photo filenames.\n"
            "2. Use read_policy_schedule on that policy number to confirm the "
            "policy exists and note the registered vehicle.\n"
            "3. Take the invoice from the previous task.\n\n"
            "Derive `damaged_parts` from the invoice line items: list the "
            "physical parts billed for replacement, lowercase and without the "
            "word 'replacement' (for example 'front bumper', 'headlight'). "
            "Exclude painting and labour lines - they are not parts.\n\n"
            "Set `is_complete` to false and name the gaps in `missing_fields` "
            "if any of these hold:\n"
            "- the policy number does not resolve to a schedule\n"
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
    )


def run_intake(claim_id: str, verbose: bool = True) -> ClaimIntake:
    crew = build_intake_crew(verbose=verbose)
    result = crew.kickoff(inputs={"claim_id": claim_id})
    return result.pydantic
