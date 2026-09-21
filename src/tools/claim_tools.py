"""
Custom tools for the Intake crew.

Deliberately dumb: they fetch bytes and return text. Interpretation is the
agent's job, extraction of ground truth is not the tool's.
"""

import json
from pathlib import Path
from typing import Type

import pdfplumber
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent.parent
INVOICES = ROOT / "data" / "invoices"
POLICIES = ROOT / "data" / "policies"
CLAIMS = ROOT / "data" / "claims"


class ClaimIdInput(BaseModel):
    claim_id: str = Field(..., description="Claim identifier, e.g. CLM-5000")


class PolicyNoInput(BaseModel):
    policy_no: str = Field(..., description="Policy number, e.g. POL-3000")


class ReadInvoicePDF(BaseTool):
    name: str = "read_invoice_pdf"
    description: str = (
        "Read the repair invoice PDF for a claim and return its text content, "
        "including the line items table. Input: claim_id such as CLM-5000."
    )
    args_schema: Type[BaseModel] = ClaimIdInput

    def _run(self, claim_id: str) -> str:
        path = INVOICES / f"{claim_id}.pdf"
        if not path.exists():
            return f"ERROR: no invoice found at {path.name}"
        try:
            with pdfplumber.open(path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception as exc:                      # noqa: BLE001
            return f"ERROR: could not read {path.name}: {exc}"
        return text.strip() or "ERROR: invoice contained no extractable text"


class ReadPolicySchedule(BaseTool):
    name: str = "read_policy_schedule"
    description: str = (
        "Look up the policy schedule for a policy number. Returns JSON with "
        "the insured's vehicle, IDV, deductible, cover dates and add-ons. "
        "Input: policy_no such as POL-3000."
    )
    args_schema: Type[BaseModel] = PolicyNoInput

    def _run(self, policy_no: str) -> str:
        path = POLICIES / f"{policy_no}.json"
        if not path.exists():
            return f"ERROR: no policy on file for {policy_no}"
        return path.read_text()


class ReadClaimSubmission(BaseTool):
    name: str = "read_claim_submission"
    description: str = (
        "Read what the claimant submitted: policy number, incident date, "
        "narrative, and the filenames of any photos. Does NOT include the "
        "invoice contents. Input: claim_id such as CLM-5000."
    )
    args_schema: Type[BaseModel] = ClaimIdInput

    def _run(self, claim_id: str) -> str:
        path = CLAIMS / f"{claim_id}.json"
        if not path.exists():
            return f"ERROR: no claim on file for {claim_id}"
        claim = json.loads(path.read_text())
        submitted = {
            "claim_id": claim["claim_id"],
            "policy_no": claim["policy_no"],
            "incident_date": claim["incident_date"],
            "narrative": claim["narrative"],
            "photos": claim.get("photos", []),
        }
        return json.dumps(submitted, indent=2)
