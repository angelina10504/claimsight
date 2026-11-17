"""
Does a cited clause APPLY to this policy?

citations.py answers "is the cited text in the policy wording?". That is not
enough. The wording is the full India Motor Tariff: besides the Private Car
Package Policy it carries endorsements for commercial vehicles, motor trade,
two-wheelers and special vehicles, and add-ons that apply only if bought.
"IMT 21" is real, correctly quoted text, and it excludes bumpers and paint work
- for "all Commercial Vehicles". Quoted against a private car it underpays the
claim, and every existence check passes. This module checks scope.

No model is involved. The scoped blocks are listed below with the scope their
own heading states; their line ranges are read from the wording itself, so an
edited wording cannot silently shift a range.

    POLICY_FORM        the form every policy in this dataset is issued on
    SCOPED             the 51 blocks whose heading limits who they apply to
    cited_blocks()     which scoped blocks a piece of text cites
    applicability()    can that block apply to this policy, and why not
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
WORDING = ROOT / "knowledge" / "policy_wording.txt"

# ---------------------------------------------------------------- the policy form
# The vehicle class is NOT recorded on any policy (data/policies/*.json has make,
# model, idv, ... but no class). It is constant here ONLY because this dataset
# issues every policy on one form: line 3 of the wording, "STANDARD FORM FOR
# PRIVATE CAR PACKAGE POLICY". Future work for a real insurer: a per-policy
# vehicle_class field, read by policy_form() below instead of this constant.
POLICY_FORM = "Private Car Package Policy"
POLICY_FORM_HEADING = "STANDARD FORM FOR PRIVATE CAR PACKAGE POLICY"


def policy_form(policy: Optional[dict] = None) -> str:
    """The form this policy is issued on. Reads a per-policy field when one
    exists; this dataset has none, so it falls back to the one form."""
    return (policy or {}).get("policy_form") or POLICY_FORM


# ---------------------------------------------------------------- the scope map
# Rules, per block:
#   cannot        the heading's scope excludes a private car outright
#   if_purchased  an add-on: applies only if the policy records the purchase
#                 (only zero_depreciation is recorded; the others cannot apply)
#   if_attached   a private-car-capable IMT endorsement that must be attached to
#                 the policy. Attachment is not recorded anywhere, so each one
#                 is decided below in ATTACHED (approved 2026-11-18).
CANNOT, IF_PURCHASED, IF_ATTACHED = "cannot", "if_purchased", "if_attached"

# GOVERNING PRINCIPLE - it decides every future entry in this map.
#   Over-blocking kills a claim; under-blocking sends it to a human. Blocking a
#   clause here makes the guardrail refuse a decision that relies on it, and
#   after the retries the claim cannot be completed at all. Letting a clause
#   through only risks a wrong denial, and the routing rule in
#   claim_flow.coverage_doubts sends any line denied on a scoped clause to a
#   person. So when the data does not settle whether a clause applies, be
#   permissive and let the routing rule catch it.
#   The exceptions are clauses that nothing in the data supports AND that would
#   refuse every accident claim if wrongly applied: IMT 26 and IMT 27 restrict
#   Section I to fire and theft. Those are blocked.
#
# The if_attached endorsements recorded as attached to every policy. IMT 22 (the
# compulsory deductible for private cars) is attached: each policy records a real
# `deductible`. Every other if_attached endorsement - IMT 26 and 27 above all -
# is treated as not attached: no policy records it, and none was sold here.
ATTACHED = {"IMT 22"}

# id -> (rule, category, scope as the heading states it)
SCOPED: Dict[str, Tuple[str, str, str]] = {
    # commercial / goods / passenger-carrying
    "IMT 21": (CANNOT, "commercial", "Commercial Vehicles"),
    "IMT 23": (CANNOT, "commercial", "Commercial Vehicles"),
    "IMT 34": (CANNOT, "commercial", "Commercial Vehicle Policies"),
    "IMT 37": (CANNOT, "commercial", "Commercial Vehicles"),
    "IMT 37.A": (CANNOT, "commercial", "Commercial Vehicles"),
    "IMT 38": (CANNOT, "commercial", "Commercial and Motor Trade Vehicles"),
    "IMT 39": (CANNOT, "goods", "Goods Vehicles"),
    "IMT 40": (CANNOT, "passenger-carrying", "buses, taxis and commercial three/four wheelers"),
    "IMT 42": (CANNOT, "goods", "Goods Carrying Commercial Vehicles"),
    "IMT 46": (CANNOT, "special-vehicle", "Ambulances/Hearses and Motor Trade vehicles"),
    # motor trade
    "IMT 41": (CANNOT, "motor-trade", "Motor Trade Policies (Class F)"),
    "IMT 57": (CANNOT, "motor-trade", "Motor Trade Policies"),
    "IMT 58": (CANNOT, "motor-trade", "Motor Trade Policies"),
    "IMT 59": (CANNOT, "motor-trade", "Motor Trade Policies"),
    "IMT 60": (CANNOT, "motor-trade", "Motor Trade Policies"),
    "IMT 61": (CANNOT, "motor-trade", "Motor Trade Policies"),
    "IMT 62": (CANNOT, "motor-trade", "Motor Trade Road Transit Risks Policies"),
    "IMT 63": (CANNOT, "motor-trade", "Motor Trade Internal Risks Policies"),
    "IMT 64": (CANNOT, "motor-trade", "Motor Trade Internal Risks Policies"),
    "IMT 65": (CANNOT, "motor-trade", "Motor Trade Internal Risks Policies"),
    # two-wheelers
    "IMT 18": (CANNOT, "two-wheeler", "Motorised Two Wheelers"),
    "IMT 33": (CANNOT, "two-wheeler", "Motorised Two Wheeler Policies"),
    # special vehicles
    "IMT 47": (CANNOT, "special-vehicle", "mobile cranes, drilling rigs and mobile plant"),
    "IMT 48": (CANNOT, "special-vehicle", "agricultural, forestry and miscellaneous vehicles"),
    "IMT 51": (CANNOT, "special-vehicle", "mobile shops, canteens and surgeries"),
    "IMT 53": (CANNOT, "special-vehicle", "Special Type Vehicles"),
    "IMT 56": (CANNOT, "special-vehicle", "trailers in road transit"),
    # private-car-capable endorsements that must be attached (default undecided)
    "IMT 2": (IF_ATTACHED, "private-car-endorsement", "vintage cars"),
    "IMT 8": (IF_ATTACHED, "private-car-endorsement", "private cars and two wheelers (association members)"),
    "IMT 9": (IF_ATTACHED, "private-car-endorsement", "vintage private cars"),
    "IMT 10": (IF_ATTACHED, "private-car-endorsement", "policies other than motor trade (anti-theft device)"),
    "IMT 15": (IF_ATTACHED, "private-car-endorsement", "private cars and two wheelers not for hire"),
    "IMT 16": (IF_ATTACHED, "private-car-endorsement", "vehicles not for hire or reward"),
    "IMT 22": (IF_ATTACHED, "private-car-endorsement", "private cars and two wheelers (compulsory deductible)"),
    "IMT 22.A": (IF_ATTACHED, "private-car-endorsement", "private cars and two wheelers not for hire"),
    "IMT 24": (IF_ATTACHED, "private-car-endorsement", "non-factory electrical fittings, package policies"),
    "IMT 26": (IF_ATTACHED, "private-car-endorsement", "fire and/or theft risks only policies"),
    "IMT 27": (IF_ATTACHED, "private-car-endorsement", "liability and fire and/or theft only policies"),
    "IMT 29": (IF_ATTACHED, "private-car-endorsement", "vehicles not for hire or reward"),
    "IMT 30": (IF_ATTACHED, "private-car-endorsement", "private cars towing trailers"),
    "IMT 31": (IF_ATTACHED, "private-car-endorsement", "private cars and two wheelers in rallies"),
    "IMT 35": (IF_ATTACHED, "private-car-endorsement", "four-wheelers up to 6 seats hired out"),
    # add-ons: apply only if bought
    "ADD-ON Zero Depreciation": (IF_PURCHASED, "add-on", "zero_depreciation"),
    "ADD-ON Consumable Items": (IF_PURCHASED, "add-on", ""),
    "ADD-ON Garage Cash": (IF_PURCHASED, "add-on", ""),
    "ADD-ON NCB Protect": (IF_PURCHASED, "add-on", ""),
    "ADD-ON Return to Invoice": (IF_PURCHASED, "add-on", ""),
    "ADD-ON Engine Protect Plus": (IF_PURCHASED, "add-on", ""),
    "ADD-ON Road Side Assistance": (IF_PURCHASED, "add-on", ""),
    "ADD-ON Key Protect": (IF_PURCHASED, "add-on", ""),
    "ADD-ON Loss of Personal Belongings": (IF_PURCHASED, "add-on", ""),
}

ADD_ON_TITLES = [k[len("ADD-ON "):] for k in SCOPED if k.startswith("ADD-ON ")]
_IMT_HEAD = re.compile(r"^IMT\.\s?(\d+)(?:\.\s?([A-C])\.)?")


def _normalise(text: str) -> str:
    """Exactly as citations.wording() normalises, so offsets line up."""
    for fancy, plain in {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"}.items():
        text = text.replace(fancy, plain)
    return " ".join(text.lower().split())


@lru_cache(maxsize=1)
def _layout() -> Tuple[str, List[dict]]:
    """The normalised wording, and every block (scoped or not) with its line range
    and its character range in the normalised text. Ranges run from one block
    heading to the next, so a scoped block never swallows an unscoped one."""
    lines = WORDING.read_text(errors="ignore").split("\n")
    starts = []
    for i, line in enumerate(lines):
        s = line.strip()
        m = _IMT_HEAD.match(s)
        if m:
            starts.append((i, f"IMT {m.group(1)}" + (f".{m.group(2)}" if m.group(2) else "")))
        elif s in ADD_ON_TITLES:
            starts.append((i, f"ADD-ON {s}"))
        elif s.startswith("ADD-ONS FOR COMPREHENSIVE"):
            starts.append((i, "ADD-ONS SECTION"))       # ends IMT 65; not itself scoped
    # character offset of each line in the normalised text
    offsets, pos = [], 0
    for line in lines:
        norm = _normalise(line)
        offsets.append(pos)
        if norm:
            pos += len(norm) + 1
    text = _normalise("\n".join(lines))
    blocks = []
    for (a, ident), (b, _) in zip(starts, starts[1:] + [(len(lines), "END")]):
        blocks.append({"id": ident, "first_line": a + 1, "last_line": b,
                       "start": offsets[a], "end": offsets[b] if b < len(lines) else len(text)})
    return text, blocks


def scoped_blocks() -> List[dict]:
    """The 51 scoped blocks with line range, rule, category and stated scope."""
    _, blocks = _layout()
    out = []
    for b in blocks:
        if b["id"] in SCOPED:
            rule, category, scope = SCOPED[b["id"]]
            out.append({**b, "rule": rule, "category": category, "scope": scope})
    return out


def block_at(position: int) -> Optional[str]:
    """The block holding this offset of the normalised wording, or None for the
    base policy text before the first endorsement."""
    for b in _layout()[1]:
        if b["start"] <= position < b["end"]:
            return b["id"]
    return None


# ---------------------------------------------------------------- what a reason cites
_IMT_REF = re.compile(r"\bimt\.?\s*-?\s*(\d{1,2})(?:\s*\.?\s*([a-c])\b)?", re.I)
_PIECES = re.compile(r"[\"“”'‘’()\[\]]|…|\.\.\.")
WINDOW = 1200          # how close quoted fragments must sit to count as one clause


def _verbatim_locations(reason: str) -> List[int]:
    """Where the reason's verbatim text sits in the wording.

    An agent quotes with ellipses ("the insurer shall not be liable … paint
    work"), so the reason is cut into pieces and the pieces are located
    together: an anchor position where every other found piece lies close by.
    Pieces shorter than 8 characters are ignored; generic ones like "for loss
    of or damage to" occur everywhere, which is why the pieces must co-occur."""
    text, _ = _layout()
    pieces = []
    for raw in _PIECES.split(reason or ""):
        for part in re.split(r":\s|;\s", raw):
            p = _normalise(part).strip(" .,:;-")
            if len(p) >= 8 and p in text:
                pieces.append(p)
    if not pieces:
        return []
    pieces = sorted(set(pieces), key=len, reverse=True)
    # the rarest piece is the best anchor
    anchor = min(pieces, key=lambda p: text.count(p))
    found = []
    for m in re.finditer(re.escape(anchor), text):
        here = m.start()
        nearby = text[max(0, here - WINDOW):here + WINDOW]
        if all(p in nearby for p in pieces):
            found.append(here)
    return found


def cited_blocks(reason: str) -> List[str]:
    """The scoped blocks a reason cites: by naming an IMT endorsement, or by
    quoting text that is found only inside scoped blocks. Text that also occurs
    in the base policy is not attributed to a scoped block."""
    cited = []
    for m in _IMT_REF.finditer(reason or ""):
        ident = f"IMT {int(m.group(1))}" + (f".{m.group(2).upper()}" if m.group(2) else "")
        if ident in SCOPED and ident not in cited:
            cited.append(ident)
    places = _verbatim_locations(reason)
    holders = {block_at(p) for p in places}
    if places and holders <= set(SCOPED):          # every location is inside scoped blocks
        for ident in sorted(h for h in holders if h):
            if ident not in cited:
                cited.append(ident)
    return cited


# ---------------------------------------------------------------- can it apply?
def applicability(ident: str, policy: Optional[dict] = None) -> Tuple[bool, str]:
    """(can apply, reason). Unscoped text always can."""
    if ident not in SCOPED:
        return True, ""
    rule, _category, scope = SCOPED[ident]
    form = policy_form(policy)
    if rule == CANNOT:
        return False, f"{ident} applies only to {scope}; this policy is a {form}."
    if rule == IF_PURCHASED:
        name = ident[len("ADD-ON "):]
        if scope and (policy or {}).get(scope):
            return True, ""
        return False, (f"the {name} add-on applies only if it was purchased; "
                       f"this policy records no purchase of it.")
    # IF_ATTACHED: applies only if this policy carries the endorsement
    if ident in ATTACHED:
        return True, ""
    return False, (f"{ident} ({scope}) applies only if attached to the policy; "
                   f"no attachment of it is recorded on this {form}.")


def inapplicable(reason: str, policy: Optional[dict] = None) -> List[str]:
    """Why each scoped clause this reason cites cannot apply. Empty when it cites
    none, or only clauses that can apply."""
    out = []
    for ident in cited_blocks(reason):
        ok, why = applicability(ident, policy)
        if not ok:
            out.append(why)
    return out


def attachment_endorsements() -> List[dict]:
    """The if_attached blocks, each with whether it is treated as attached."""
    return [{**b, "attached": b["id"] in ATTACHED}
            for b in scoped_blocks() if b["rule"] == IF_ATTACHED]
