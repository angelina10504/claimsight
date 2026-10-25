"""
ClaimSight MCP server.

Exposes the claim tools over the Model Context Protocol, so any MCP client can
use the same implementation: the CrewAI Assessment crew today, and the ADK
Claimant Assistant later. One tool implementation, two agent frameworks.

Tools
  check_policy_period    is the incident inside the policy's cover dates?
  check_part_prices      is any invoice line far above the market rate?
  find_prior_claims      has the same damage been claimed before?
  calculate_settlement   payout under the policy's depreciation rules
  get_claim_status       where a claim stands (for the claimant assistant)

Every tool validates its inputs (types come from the signatures; values are
checked below) and returns JSON. A bad input returns {"error": ...} instead of
crashing, so the calling agent sees what went wrong and can correct itself.

Run:  uv run python src/mcp_server.py           stdio (CrewAI launches it this way)
      uv run python src/mcp_server.py --http    HTTP on port 8765, for remote clients
"""

import json
import re
import sys
import warnings
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

# A harmless warning from inside the MCP SDK's settings model; hide it so it
# does not clutter the agent logs.
warnings.filterwarnings("ignore", message=".*'lifespan' has an incomplete definition.*")

from mcp.server.fastmcp import FastMCP   # noqa: E402

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC / "tools"))

import assessment_tools as tools   # noqa: E402  (the same functions the crews used directly)
import guardrails                  # noqa: E402  (PII redaction before a report leaves)
import reporting                   # noqa: E402  (the evidence report, built from saved files)
from assessment_tools import PriceLine, SettlementLine   # noqa: E402  (typed invoice lines)

DECISIONS = ROOT / "data" / "decisions"
REVIEW_QUEUE = ROOT / "data" / "review_queue"

POLICY_RE = re.compile(r"^POL-\d{4}$")
CLAIM_RE = re.compile(r"^CLM-\d{4}$")

mcp = FastMCP("claimsight-tools", port=8765, log_level="WARNING")


# ------------------------------------------------------------ input checks
def _bad_policy(policy_no: str) -> Optional[dict]:
    if not POLICY_RE.match(policy_no or ""):
        return {"error": f"policy_no must look like POL-1234, got {policy_no!r}"}
    return None


def _bad_claim(claim_id: str) -> Optional[dict]:
    if not CLAIM_RE.match(claim_id or ""):
        return {"error": f"claim_id must look like CLM-1234, got {claim_id!r}"}
    return None


def _bad_date(value: str) -> Optional[dict]:
    try:
        date.fromisoformat(value)
        return None
    except (TypeError, ValueError):
        return {"error": f"date must be YYYY-MM-DD, got {value!r}"}


def _as_dicts(lines) -> List[Dict[str, Any]]:
    """Typed invoice lines (pydantic models) back to plain dicts for the calculators."""
    return [line.model_dump(exclude_none=True) if hasattr(line, "model_dump") else line
            for line in (lines or [])]


def _bad_lines(lines: List[Dict[str, Any]]) -> Optional[dict]:
    if not lines:
        return {"error": "line_items is empty"}
    for n, line in enumerate(lines, 1):
        if not isinstance(line, dict) or not str(line.get("description", "")).strip():
            return {"error": f"line {n} has no description"}
        try:
            amount = float(line.get("amount"))
        except (TypeError, ValueError):
            return {"error": f"line {n} amount is not a number: {line.get('amount')!r}"}
        if amount < 0:
            return {"error": f"line {n} amount is negative"}
    return None


# ------------------------------------------------------------ tools
@mcp.tool()
def check_policy_period(policy_no: str, incident_date: str) -> dict:
    """Check whether the incident date falls inside the policy's cover period.
    Claims for incidents outside the period are not covered.

    policy_no: e.g. POL-3004.  incident_date: YYYY-MM-DD."""
    return (_bad_policy(policy_no) or _bad_date(incident_date)
            or tools.check_policy_period(policy_no, incident_date))


@mcp.tool()
def check_part_prices(line_items: List[PriceLine]) -> dict:
    """Compare each billed part with its market price, and check that painting
    and labour are in a normal proportion. Returns the lines that look inflated.

    line_items: every invoice line, each {"description": str, "amount": number}."""
    lines = _as_dicts(line_items)
    return _bad_lines(lines) or tools.check_part_prices(lines)


@mcp.tool()
def find_prior_claims(policy_no: str, claim_id: str, incident_date: str,
                      damaged_parts: List[str]) -> dict:
    """Look up earlier claims on the same policy in the last 180 days and report
    any that billed the same parts, which suggests a duplicate claim.

    damaged_parts: e.g. ["rear bumper", "headlight"]."""
    return (_bad_policy(policy_no) or _bad_claim(claim_id) or _bad_date(incident_date)
            or tools.find_prior_claims(policy_no, claim_id, incident_date, damaged_parts))


@mcp.tool()
def calculate_settlement(policy_no: str, line_items: List[SettlementLine]) -> dict:
    """Compute the payout under the policy's depreciation, GST, deductible and
    IDV rules. Pass EVERY invoice line; set "admitted": false (with a "reason")
    on lines that should not be paid. Always use this for amounts.

    line_items: each {"description", "amount", "material"?, "admitted"?, "reason"?}."""
    lines = _as_dicts(line_items)
    return _bad_policy(policy_no) or _bad_lines(lines) or tools.settle(policy_no, lines)


MAX_EVIDENCE = 20          # a report is for reading, not for dumping a whole log
MAX_DETAIL = 300


@mcp.tool()
def get_claim_report(claim_id: str) -> dict:
    """Why a claim was decided the way it was: the coverage finding and the
    clause behind it, the fraud checks, every invoice line with its reason, the
    settlement breakdown, what evidence is still needed, and where each finding
    came from. Use this when a claimant asks why, not to decide anything.

    Built from the claim's saved decision and monitoring log, so it calls no
    model. Personal data is removed before the report leaves this server."""
    bad = _bad_claim(claim_id)
    if bad:
        return bad
    report = reporting.for_claim(claim_id)
    if report is None:
        return {"error": f"no assessed claim {claim_id}; it has not been through the Flow yet"}

    data = report.model_dump()
    data["explanation"] = reporting.explanation(report)
    trimmed = data["evidence"][:MAX_EVIDENCE]
    for item in trimmed:
        item["detail"] = str(item.get("detail", ""))[:MAX_DETAIL]
    data["evidence"] = trimmed
    data["evidence_truncated"] = len(report.evidence) > MAX_EVIDENCE

    clean, removed = guardrails.redact_pii(json.dumps(data, default=str))
    data = json.loads(clean)
    data["pii_redacted"] = removed
    return data


@mcp.tool()
def get_claim_status(claim_id: str) -> dict:
    """Where a claim stands: approved, rejected, awaiting review, awaiting
    evidence, or not yet processed. Returns the payout once decided."""
    bad = _bad_claim(claim_id)
    if bad:
        return bad
    for folder, fallback in ((DECISIONS, None), (REVIEW_QUEUE, "awaiting_review")):
        path = folder / f"{claim_id}.json"
        if path.exists():
            state = json.loads(path.read_text())
            return {
                "claim_id": claim_id,
                "status": state.get("status") or fallback,
                "route": state.get("route"),
                "payout": state.get("final_payout", 0.0),
                "missing_evidence": (state.get("intake") or {}).get("missing_fields", []),
            }
    return {"claim_id": claim_id, "status": "not_processed"}


if __name__ == "__main__":
    mcp.run(transport="streamable-http" if "--http" in sys.argv else "stdio")
