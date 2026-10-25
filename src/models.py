"""
Typed contracts between crews.

Every agent hands off a validated object rather than free text. The routers
later branch on these fields, so if a stage cannot produce a valid object the
Flow stops there instead of passing a hallucinated string downstream.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


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
    is_covered: bool = Field(
        ..., description="False if the incident is outside the policy period or not an insured peril"
    )
    coverage_reasons: List[str] = Field(
        default_factory=list,
        description="Each coverage finding with the policy clause it rests on",
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
