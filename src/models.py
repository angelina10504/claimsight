"""
Typed contracts between crews.

Every agent hands off a validated object rather than free text. The routers
later branch on these fields, so if a stage cannot produce a valid object the
Flow stops there instead of passing a hallucinated string downstream.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class LineItem(BaseModel):
    description: str = Field(..., description="The line as printed on the invoice")
    amount: float = Field(..., description="Rupee amount for this line, no symbols")
    material: Optional[str] = Field(
        None,
        description="One of plastic, metal, glass, fibreglass, tyre, battery, "
                    "paint, labour. Leave null if not determinable.",
    )


class ExtractedInvoice(BaseModel):
    invoice_no: Optional[str] = None
    invoice_date: Optional[str] = Field(None, description="ISO date, YYYY-MM-DD")
    garage_name: Optional[str] = None
    registration: Optional[str] = Field(None, description="Vehicle registration number")
    line_items: List[LineItem] = Field(default_factory=list)
    subtotal: Optional[float] = None
    gst: Optional[float] = None
    total: Optional[float] = None


class ClaimIntake(BaseModel):
    """The output of the Intake crew: everything known before assessment."""

    claim_id: str
    policy_no: str
    registration: Optional[str] = None
    incident_date: Optional[str] = None
    narrative: Optional[str] = None
    damaged_parts: List[str] = Field(
        default_factory=list,
        description="Parts the evidence shows as damaged, lowercase, e.g. "
                    "'front bumper'",
    )
    invoice: ExtractedInvoice
    is_complete: bool = Field(
        ..., description="True if the claim has enough evidence to assess"
    )
    missing_fields: List[str] = Field(
        default_factory=list,
        description="What must be supplied before assessment can proceed",
    )
    notes: Optional[str] = Field(
        None, description="Anything the adjuster should know, one or two lines"
    )


class LineDecision(BaseModel):
    description: str = Field(..., description="The invoice line exactly as printed")
    amount: float = Field(..., description="Rupee amount for this line, before GST")
    material: Optional[str] = Field(
        None,
        description="One of plastic, metal, glass, fibreglass, tyre, battery, paint, labour",
    )
    admitted: bool = Field(..., description="True if this line should be paid")
    reason: str = Field("", description="Why the line is not admitted; empty if admitted")


class AssessmentDecision(BaseModel):
    """The output of the Assessment crew. The payout is NOT here on purpose:
    the Flow computes it from line_decisions with the settlement calculator."""

    claim_id: str
    coverage_status: Literal["covered", "excluded", "uncertain"] = Field(
        ...,
        description="covered: an insured peril inside the policy period. "
                    "excluded: a named clause or the policy period rules it out. "
                    "uncertain: the policy wording could not settle it, or the "
                    "evidence conflicts. Never guess between covered and excluded: "
                    "say uncertain and a person will decide.",
    )
    uncertainty_reasons: List[str] = Field(
        default_factory=list,
        description="If coverage_status is uncertain, what could not be established "
                    "and what would settle it. Empty otherwise.",
    )
    is_covered: bool = Field(
        False,
        description="Derived from coverage_status; do not set it yourself.",
    )
    coverage_reasons: List[str] = Field(
        default_factory=list,
        description="Each coverage finding, naming the clause it rests on "
                    "(e.g. 'Section II exclusion 4') or quoting the policy wording",
    )
    fraud_score: float = Field(
        ..., description="0.0 means no sign of fraud, 1.0 means certain fraud"
    )
    fraud_flags: List[str] = Field(
        default_factory=list, description="Each fraud signal found, one line each"
    )
    line_decisions: List[LineDecision] = Field(
        ..., description="Every invoice line, each marked admitted or not"
    )
    recommended_route: str = Field(
        ..., description="One of: auto_approve, human_review, reject"
    )
    summary: str = Field(..., description="Two or three sentences for the adjuster")

    @model_validator(mode="after")
    def _derive_is_covered(self):
        """is_covered is never taken from the model: it follows coverage_status.
        Only 'covered' pays. 'uncertain' is not a yes, so nothing is paid on it
        until a person confirms coverage."""
        object.__setattr__(self, "is_covered", self.coverage_status == "covered")
        return self


# --------------------------------------------------------------- the claim report
# These describe a claim AFTER it has been assessed. Nothing here is produced by
# an LLM: src/reporting.py assembles them from the Flow's saved state and the
# claim's monitoring log, so every statement traces back to something recorded.

class EvidenceReference(BaseModel):
    """One piece of evidence, and where it came from."""

    kind: Literal["document", "policy_clause", "tool", "guardrail", "analysis", "photo"]
    source: str = Field(..., description="The document, clause, tool or agent it came from")
    detail: str = Field(..., description="What it actually said")
    grounded: Optional[bool] = Field(
        None,
        description="For a policy clause: whether the citation was verified against "
                    "the policy wording. None where the check does not apply.",
    )
    at_seconds: Optional[float] = Field(
        None, description="Seconds into the run, from the monitoring log"
    )


class EvidenceRequest(BaseModel):
    """Something still missing, and who can supply it."""

    item: str = Field(..., description="The exact evidence needed, e.g. 'photograph of the windshield'")
    why: str = Field(..., description="What it would settle")
    from_whom: Literal["claimant", "adjuster"] = "claimant"


class ClaimReport(BaseModel):
    """Everything an assessor needs to check a ClaimSight decision."""

    claim_id: str
    policy_no: str = ""
    incident_date: Optional[str] = None
    registration: Optional[str] = None

    coverage_status: str = ""
    coverage_reasons: List[EvidenceReference] = Field(default_factory=list)
    coverage_doubts: List[str] = Field(default_factory=list)

    fraud_score: float = 0.0
    fraud_flags: List[str] = Field(default_factory=list)

    line_decisions: List[LineDecision] = Field(default_factory=list)

    payout_doubts: List[str] = Field(
        default_factory=list,
        description="Reasons the computed payout should not be acted on as it stands",
    )
    settlement_explanation: str = ""
    proposed_payout: float = Field(
        0.0, description="What the calculator says the claim is worth as assessed")
    settlement_state: str = Field(
        "", description="pending_human_review, pending_evidence, authorised, "
                        "refused, or not_assessed",
    )
    authorised_payout: Optional[float] = Field(
        None,
        description="What a person has actually authorised. None while nobody has "
                    "decided: a nought there would read as a refusal.",
    )
    final_payout: float = Field(
        0.0, description="Kept for older readers; 0.0 while nothing is authorised. "
                         "Use authorised_payout and settlement_state instead.")

    evidence: List[EvidenceReference] = Field(
        default_factory=list, description="Every document, tool result and guardrail check used"
    )
    outstanding_evidence: List[EvidenceRequest] = Field(default_factory=list)

    route: str = ""
    status: str = ""
    route_reason: str = ""
    guardrail_flags: List[str] = Field(default_factory=list)

    run: dict = Field(default_factory=dict, description="Seconds, tokens and tool calls for the run")

