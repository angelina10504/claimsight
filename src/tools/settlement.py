"""
Deterministic settlement calculator for ClaimSight.

Implements the depreciation and deductible rules from the Private Car Package
Policy wording (ICICI Lombard, IRDAI repository):

  Section I, "Subject to a deduction for depreciation ... in respect of parts
  replaced":
      rubber / nylon / plastic / tyres / batteries ......... 50%
      fibre glass components .............................. 30%
      glass ............................................... Nil
      all other parts ..................... age-band table below
  Rate of Depreciation for Painting: 50% applied only on the material cost;
  for a consolidated painting charge the material component is taken as 25%
  of the total, giving an effective 12.5%.
  Labour attracts no depreciation.
  Zero Depreciation add-on waives the deduction on parts.
  The deductible stated in the schedule is subtracted from every claim.
  Liability is capped at the IDV.

No LLM touches these numbers. An agent decides WHICH lines are admitted; this
module decides what those lines are worth.
"""

from dataclasses import dataclass, field, asdict

GST = 0.18

AGE_BANDS = [
    (0.5, 0.00), (1, 0.05), (2, 0.10), (3, 0.15),
    (4, 0.25), (5, 0.35), (10, 0.40), (99, 0.50),
]

FLAT_RATES = {
    "plastic": 0.50,
    "rubber": 0.50,
    "tyre": 0.50,
    "battery": 0.50,
    "fibreglass": 0.30,
    "glass": 0.00,
    "paint": 0.125,
    "labour": 0.00,
}

# keyword -> material, used when the caller supplies only a description
KEYWORDS = {
    "bumper": "plastic", "headlight": "plastic", "taillight": "plastic",
    "tail light": "plastic", "head light": "plastic", "mirror": "plastic",
    "grille": "plastic", "grill": "plastic", "cladding": "plastic",
    "moulding": "plastic", "tyre": "tyre", "tube": "tyre",
    "battery": "battery", "windshield": "glass", "windscreen": "glass",
    "glass": "glass", "paint": "paint", "painting": "paint",
    "labour": "labour", "labor": "labour", "service charge": "labour",
    "bonnet": "metal", "door": "metal", "fender": "metal",
    "panel": "metal", "boot": "metal", "roof": "metal", "pillar": "metal",
}


def classify_material(description: str) -> str:
    """Best-effort material from a free-text invoice line. Defaults to metal."""
    text = description.lower()
    for keyword, material in KEYWORDS.items():
        if keyword in text:
            return material
    return "metal"


def metal_depreciation(age_years: float) -> float:
    for limit, rate in AGE_BANDS:
        if age_years <= limit:
            return rate
    return 0.50


def depreciation_rate(material: str, age_years: float) -> float:
    material = (material or "").lower()
    if material in FLAT_RATES:
        return FLAT_RATES[material]
    return metal_depreciation(age_years)


@dataclass
class LineResult:
    description: str
    material: str
    amount: float
    depreciation_rate: float
    depreciation_amount: float
    payable: float


@dataclass
class Settlement:
    admitted_subtotal: float
    total_depreciation: float
    after_depreciation: float
    gst: float
    deductible: float
    payout: float
    capped_at_idv: bool
    lines: list = field(default_factory=list)
    excluded: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def calculate_settlement(line_items, policy, gst_rate: float = GST) -> Settlement:
    """Compute the payout for a set of invoice lines under one policy.

    line_items: list of dicts with at least `description` and `amount`.
                Optional `material` (inferred if absent) and `admitted`
                (defaults True). Non-admitted lines are reported separately
                and contribute nothing.
    policy:     dict with vehicle_age_years, deductible, idv,
                zero_depreciation.
    """
    age = float(policy.get("vehicle_age_years", 0))
    zero_dep = bool(policy.get("zero_depreciation", False))
    deductible = float(policy.get("deductible", 0))
    idv = float(policy.get("idv", float("inf")))

    lines, excluded = [], []
    subtotal = 0.0
    depreciation_total = 0.0

    for item in line_items:
        amount = float(item["amount"])
        material = item.get("material") or classify_material(item["description"])

        if not item.get("admitted", True):
            excluded.append({"description": item["description"],
                             "amount": amount,
                             "reason": item.get("reason", "not admitted")})
            continue

        # Zero Dep waives depreciation on parts, not on the nil-rated lines
        rate = 0.0 if zero_dep else depreciation_rate(material, age)
        deduction = round(amount * rate, 2)
        payable = round(amount - deduction, 2)

        subtotal += amount
        depreciation_total += deduction
        lines.append(LineResult(item["description"], material, amount,
                                rate, deduction, payable))

    after_dep = round(subtotal - depreciation_total, 2)
    gst = round(after_dep * gst_rate, 2)
    payout = after_dep + gst - deductible
    capped = payout > idv
    payout = round(max(0.0, min(payout, idv)), 2)

    return Settlement(
        admitted_subtotal=round(subtotal, 2),
        total_depreciation=round(depreciation_total, 2),
        after_depreciation=after_dep,
        gst=gst,
        deductible=deductible,
        payout=payout,
        capped_at_idv=capped,
        lines=[asdict(line) for line in lines],
        excluded=excluded,
    )


def explain(settlement: Settlement) -> str:
    """Human-readable breakdown for the adjuster queue and the settlement letter."""
    rows = [
        f"  {line['description']}: Rs {line['amount']:,.0f}"
        f"  - {line['depreciation_rate'] * 100:.1f}% depreciation"
        f" (Rs {line['depreciation_amount']:,.0f})"
        f"  = Rs {line['payable']:,.0f}"
        for line in settlement.lines
    ]
    out = ["Admitted lines:", *rows]
    if settlement.excluded:
        out.append("Excluded lines:")
        out += [f"  {e['description']}: Rs {e['amount']:,.0f} - {e['reason']}"
                for e in settlement.excluded]
    out += [
        f"Subtotal after depreciation: Rs {settlement.after_depreciation:,.2f}",
        f"GST: Rs {settlement.gst:,.2f}",
        f"Less deductible: Rs {settlement.deductible:,.2f}",
        f"Payout: Rs {settlement.payout:,.2f}"
        + (" (capped at IDV)" if settlement.capped_at_idv else ""),
    ]
    return "\n".join(out)
