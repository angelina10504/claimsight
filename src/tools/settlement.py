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
PAINT_MATERIAL_SHARE = 0.25      # of a consolidated painting charge
PAINT_MATERIAL_RATE = 0.50       # depreciation on that material share

# Every rule this calculator applies, and what it rests on.
#
# `basis` is an exact phrase from the policy wording where the rule comes from
# the policy, so it can be checked automatically; `reasoning` carries any step
# from that clause to this rule that is ours rather than the policy's.
# the policy: test_audit.py checks each one really is in
# knowledge/policy_wording.txt, so a rule cannot drift away from its clause
# unnoticed. Where a rule is OUR choice rather than the policy's, basis says so
# plainly, and `if_different` says what a real insurer might do instead. That
# distinction is the point: passing our own tests shows the code matches our
# model, not that our model matches every insurer.
ASSUMPTIONS = [
    {"rule": "rubber, nylon, plastic, tyres, tubes, batteries and air bags",
     "value": "50% depreciation",
     "basis": "For all rubber/ nylon / plastic parts, tyres and tubes, "
              "batteries and air bags -50%",
     "if_different": "some insurers rate airbags separately"},
    {"rule": "fibre glass components", "value": "30% depreciation",
     "basis": "For fibre glass components -30%", "if_different": ""},
    {"rule": "parts made of glass", "value": "no depreciation",
     "basis": "For all parts made of glass -Nil", "if_different": ""},
    {"rule": "all other parts, including wooden parts", "value": "by vehicle age",
     "basis": "Rate of depreciation for all other parts including wooden parts "
              "will be as per the following schedule",
     "if_different": "the age is taken from the policy schedule, not from the "
                     "registration date"},
    {"rule": "painting", "value": "12.5% depreciation",
     "basis": "the depreciation rate of 50% shall be applied only on the "
              "material cost of total painting charges",
     "if_different": "an invoice that splits paint material from paint labour "
                     "should be depreciated 50% on the material line only; we "
                     "apply the consolidated-bill rule of 50% of a 25% share"},
    {"rule": "labour", "value": "no depreciation",
     "basis": "Subject to a deduction for depreciation at the rates mentioned "
              "below in respect of parts replaced",
     "reasoning": "the deduction is on parts replaced; labour is not a part",
     "if_different": "a garage that bills fitting as part of the part line "
                     "would have that whole line depreciated"},
    {"rule": "the deductible", "value": "the amount in the policy schedule",
     "basis": "The Company shall not be liable for each and every claim under "
              "Section -I (loss of or damage to the vehicle insured) of this "
              "Policy in respect of the deductible stated in the schedule",
     "if_different": "IRDAI sets a compulsory deductible by engine size "
                     "(commonly Rs 1,000 up to 1500cc and Rs 2,000 above); a "
                     "voluntary deductible would add to it"},
    {"rule": "the cap on what is payable", "value": "the IDV",
     "basis": "The Insured's Declared Value (IDV) of the vehicle will be "
              "deemed to be the 'SUM INSURED' for the purpose of this policy",
     "if_different": "a total loss is settled at IDV outright rather than by "
                     "adding up repair lines"},
    {"rule": "Zero Depreciation add-on", "value": "waives the deduction",
     "basis": "model assumption: the add-on is a flag on the policy schedule, "
              "not a clause in this wording",
     "if_different": "real zero-dep cover usually excludes consumables and "
                     "limits how many claims it applies to"},
    {"rule": "GST at 18%", "value": "added after depreciation",
     "basis": "model assumption, NOT from the policy wording: tax treatment "
              "is a matter of tax law and of whether the insurer settles with "
              "the garage or reimburses the insured",
     "if_different": "an insurer with input tax credit may pay the pre-GST "
                     "amount; a cashless settlement pays the garage in full"},
    {"rule": "invoice amounts", "value": "taken as excluding GST",
     "basis": "model assumption: our invoices list line amounts before tax "
              "and add GST at the foot. check_invoice_tax() verifies this per "
              "invoice rather than trusting it",
     "if_different": "a tax-inclusive invoice must have the tax removed "
                     "before depreciation, or GST is counted twice"},
    {"rule": "rounding", "value": "to paise, at each step",
     "basis": "model assumption: each line's depreciation is rounded to two "
              "decimals, then GST on the rounded subtotal, then the deductible",
     "if_different": "an insurer rounding only the final figure would differ "
                     "by a few paise"},
]


def check_invoice_tax(line_items, total, gst_rate: float = GST) -> dict:
    """Do the invoice's own numbers say its line amounts include GST or not?

    This calculator depreciates each line and then adds GST, which is only
    right if the lines are pre-tax. Rather than assume that, compare the sum of
    the lines with the invoice total:

        sum == total                 the lines already include tax
        sum + GST(sum) == total      the lines are pre-tax, as expected

    An invoice that is neither should go to a person, not be guessed at."""
    amounts = [float(item["amount"]) for item in line_items or []]
    net = round(sum(amounts), 2)
    if not amounts or not total:
        return {"treatment": "unclear", "line_sum": net, "invoice_total": total,
                "note": "no line items or no invoice total to compare"}
    total = float(total)
    with_tax = round(net * (1 + gst_rate), 2)
    if abs(net - total) <= 1.0:
        return {"treatment": "inclusive", "line_sum": net, "invoice_total": total,
                "note": "the lines add up to the invoice total, so they already "
                        "include GST; depreciating them and adding GST would "
                        "count the tax twice"}
    if abs(with_tax - total) <= 2.0:
        return {"treatment": "exclusive", "line_sum": net, "invoice_total": total,
                "expected_with_gst": with_tax,
                "note": "the lines plus GST match the invoice total, which is "
                        "what this calculator expects"}
    return {"treatment": "unclear", "line_sum": net, "invoice_total": total,
            "expected_with_gst": with_tax,
            "note": f"the lines add to {net:,.2f} and the invoice says "
                    f"{total:,.2f}; neither matches with or without GST"}

AGE_BANDS = [
    (0.5, 0.00), (1, 0.05), (2, 0.10), (3, 0.15),
    (4, 0.25), (5, 0.35), (10, 0.40), (99, 0.50),
]

FLAT_RATES = {
    "plastic": 0.50,
    "rubber": 0.50,
    "tyre": 0.50,
    "battery": 0.50,
    "airbag": 0.50,          # named in the clause beside tyres and batteries
    "fibreglass": 0.30,
    "glass": 0.00,
    "paint": PAINT_MATERIAL_SHARE * PAINT_MATERIAL_RATE,   # 25% material x 50% = 12.5%
    "labour": 0.00,
}

# keyword -> material, used when the caller supplies only a description
KEYWORDS = {
    "bumper": "plastic", "headlight": "plastic", "taillight": "plastic",
    "tail light": "plastic", "head light": "plastic", "mirror": "plastic",
    "grille": "plastic", "grill": "plastic", "cladding": "plastic",
    "moulding": "plastic", "tyre": "tyre", "tube": "tyre",
    "battery": "battery", "airbag": "airbag", "air bag": "airbag",
    "srs": "airbag", "windshield": "glass", "windscreen": "glass",
    "glass": "glass", "paint": "paint", "painting": "paint",
    "labour": "labour", "labor": "labour", "service charge": "labour",
    "bonnet": "metal", "door": "metal", "fender": "metal",
    "panel": "metal", "boot": "metal", "roof": "metal", "pillar": "metal",
    # "wooden parts" fall in the age schedule, which is what metal means here
    "wood": "metal", "wooden": "metal",
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
