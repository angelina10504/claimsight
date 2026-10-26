"""
Intake crew.

Run sequentially:
  Document Reader   - turns raw invoice text into structured line items
  Claim Assembler   - merges that with the submission and policy, judges
                      completeness
  Damage Analyst    - only when the claim has photographs: records what each
                      photograph shows, part by part, as PhotoDamage

Design note: the invoice, submission and policy are fetched in plain Python and
handed to the agents as inputs. Reading a known file is not a decision, so it
is not a tool call. Keeping these agents tool-free also avoids a failure mode
where the model emits its final structured answer as a call to a tool that
does not exist. Tools are reserved for agents that genuinely choose what to
look up (the Fraud Analyst, later).

The Damage Analyst is the sixth ClaimSight agent, alongside the Document
Reader, Claims Intake Officer, Policy Analyst, Fraud Analyst and Assessment
Lead. The vision call itself is made in fetch_inputs, like reading the invoice
PDF: it is data gathering, not a choice. The agent turns that result into
PhotoDamage objects and nothing more. It never sees the invoice, and it
decides nothing.

damaged_parts is still derived from the invoice, and the photo findings are
kept apart in photo_damage: the two are compared later, in code
(check_billed_parts_against_photo), and that comparison is the point. So the
Claims Intake Officer is never shown the photo findings, and photo_damage is
attached by run_intake, not written by an agent. A claim with no photographs
runs exactly as before.
"""

import sys
from pathlib import Path

from crewai import Agent, Crew, Process, Task

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from config import get_llm, max_rpm                            # noqa: E402
from monitoring import crew_callbacks, usage_fields            # noqa: E402
import guardrails                                              # noqa: E402
import vision                                                  # noqa: E402
from models import (                                           # noqa: E402
    ClaimIntake,
    ExtractedInvoice,
    PhotoDamage,
    PhotoFindings,
)
from tools.claim_tools import (                                # noqa: E402
    ReadClaimSubmission,
    ReadInvoicePDF,
    ReadPolicySchedule,
)


PII_FIELDS = ("insured_name", "phone", "address")


def fetch_inputs(claim_id: str, monitor=None) -> dict:
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
    personal survives, safe_for_cloud raises and the claim stops here.

    A claim with photographs also gets "photo_findings": the vision model's
    verdicts on each photo. vision.inspect_photo refuses a photo that is not
    cleared for a cloud model, and the model's own notes are cleaned like any
    other text, since it may have read something off the image."""
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

    inputs = {
        "claim_id": claim_id,
        "invoice_text": invoice_text,
        "submission": guardrails.safe_for_cloud(
            json.dumps(submission, indent=2), policy_on_file,
            what=f"the claim submission for {claim_id}"),
        "policy": policy_text,
    }

    photos = submission.get("photos") or []
    if photos:
        findings = [{k: v for k, v in seen.items() if k != "visible_parts"}
                    for seen in vision.inspect_claim_photos(photos, monitor=monitor)]
        inputs["photo_findings"] = guardrails.safe_for_cloud(
            json.dumps(findings, indent=2), policy_on_file,
            what=f"the photo findings for {claim_id}")
    return inputs


def build_intake_crew(verbose: bool = True, monitor=None,
                      with_photos: bool = False) -> Crew:
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

    agents = [document_reader, claim_assembler]
    tasks = [extract_invoice, assemble_claim]

    if with_photos:
        damage_analyst = Agent(
            role="Damage Analyst",
            goal=(
                "Record what each photograph of the vehicle shows, part by "
                "part, exactly as the photo inspection reported it."
            ),
            backstory=(
                "You keep the photographic record of a motor claim. You do not "
                "see the invoice and you do not judge the claim: a part that "
                "was not visible stays not visible, because a guess written "
                "into the record would later be read as evidence."
            ),
            llm=llm,
            verbose=verbose,
            allow_delegation=False,
        )
        record_photos = Task(
            description=(
                "Below is the inspection of each photograph on claim {claim_id}, "
                "one entry per photograph.\n\n"
                "--- PHOTO INSPECTION START ---\n{photo_findings}\n"
                "--- PHOTO INSPECTION END ---\n\n"
                "Return one PhotoDamage per photograph, copying photo, damaged, "
                "undamaged, not_visible, damage_types, severity, readable and "
                "notes exactly as given. Do not move a part from one list to "
                "another, do not add or drop a part, and do not add any opinion "
                "about the claim."
            ),
            expected_output=(
                "A PhotoFindings with one PhotoDamage per photograph, matching "
                "the inspection exactly."
            ),
            agent=damage_analyst,
            context=[],              # never the invoice or the intake record
            output_pydantic=PhotoFindings,
        )
        agents.append(damage_analyst)
        tasks.append(record_photos)

    return Crew(
        agents=agents,
        tasks=tasks,
        process=Process.sequential,
        verbose=verbose,
        cache=True,
        max_rpm=max_rpm(),
        **crew_callbacks(monitor),     # step and task callbacks -> data/logs/<claim>.jsonl
    )


def photo_damage(recorded, inspected: list, monitor=None) -> list:
    """The Damage Analyst's record, checked against the inspection it copied.

    The agent only transcribes, so any difference from the inspection is the
    agent's own invention. Where its record of a photo differs in any part
    list, or a photo is missing, the inspection is used instead and the
    difference is logged."""
    by_photo = {p.photo: p for p in (getattr(recorded, "photos", None) or [])}
    checked = []
    for seen in inspected:
        expected = PhotoDamage(**{k: v for k, v in seen.items() if k != "visible_parts"})
        got = by_photo.get(seen["photo"])
        same = got is not None and all(
            set(getattr(got, f)) == set(getattr(expected, f))
            for f in ("damaged", "undamaged", "not_visible")) \
            and got.readable == expected.readable
        if not same and monitor is not None:
            monitor.record("photo_record_corrected", photo=seen["photo"],
                           missing=got is None)
        checked.append(got if same else expected)
    return checked


def run_intake(claim_id: str, verbose: bool = True, monitor=None) -> ClaimIntake:
    import json

    inputs = fetch_inputs(claim_id, monitor=monitor)
    with_photos = "photo_findings" in inputs
    crew = build_intake_crew(verbose=verbose, monitor=monitor, with_photos=with_photos)
    result = crew.kickoff(inputs=inputs)
    if monitor is not None:      # the Flow puts this in its intake crew_done entry
        monitor.crew_usage["intake"] = usage_fields(getattr(result, "token_usage", None))
    # Tasks run in order: invoice, intake record, then (with photos) the photo
    # record - so the intake is the second output, not necessarily the last.
    intake = result.tasks_output[1].pydantic
    if intake is not None:
        intake.photo_damage = (photo_damage(result.tasks_output[2].pydantic,
                                            json.loads(inputs["photo_findings"]),
                                            monitor=monitor)
                               if with_photos else [])
    return intake
